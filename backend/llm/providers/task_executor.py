"""
Task execution engine for the Deep Agents runtime.

Owns: deepagents library loading, channel wiring, tool-loop invocation,
structured/text response extraction, and the execute_task protocol method.

Agent creation and session persistence are now delegated to DeepAgentsChannel
(backend.llm.channels.deepagents_channel).  TaskExecutor's job is to:
  1. Load the deepagents library once.
  2. Create a DeepAgentsChannel (via ChannelRegistry) with all the deps it needs.
  3. Call channel.run() for every structured or text request.
  4. Parse the response text into the shape callers expect.

The proxy methods _run_deepagents_structured and _run_deepagents_text are kept
on the facade so that existing tests can patch them via patch.object(runtime, …).
All business-logic methods call these proxies (not the channel directly),
ensuring patches intercept correctly.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

from backend.llm.channels.registry import ChannelRegistry
from backend.llm.channels.deepagents_channel import DeepAgentsChannel
from backend.llm.provider_profiles import (
    ProviderProfileDocument,
    default_provider_profile_document,
    normalize_provider_profile,
)
from backend.llm.prompt_templates import build_system_prompt
from backend.llm.personas import DEFAULT_PERSONA, PERSONAS
from backend.session.models import Task, TaskPriority


# ---------------------------------------------------------------------------
# Module-level helpers (shared across this package)
# ---------------------------------------------------------------------------

def _normalize_persona(persona: str) -> str:
    candidate = (persona or "").strip().lower()
    if candidate in PERSONAS:
        return candidate
    return DEFAULT_PERSONA


def _coerce_priority(raw_priority: Any) -> TaskPriority:
    if isinstance(raw_priority, str):
        try:
            return TaskPriority(raw_priority)
        except ValueError:
            return TaskPriority.P2
    if isinstance(raw_priority, int):
        return {
            0: TaskPriority.P0,
            1: TaskPriority.P1,
            2: TaskPriority.P2,
            3: TaskPriority.P3,
        }.get(raw_priority, TaskPriority.P2)
    return TaskPriority.P2


def _extract_response_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        messages = result.get("messages", [])
        for message in reversed(messages):
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for part in reversed(content):
                    text = part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
                    if isinstance(text, str) and text.strip():
                        return text
        return json.dumps(result, ensure_ascii=True)
    return str(result)


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    matches = re.findall(r"\{.*\}", text, flags=re.DOTALL)
    for candidate in reversed(matches):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None


def _schema_to_pydantic_model(name: str, schema: dict, create_model_fn, field_cls):
    props = dict(schema.get("properties", {}))
    required = set(schema.get("required", []))
    fields = {}
    for key, prop in props.items():
        py_type = _json_schema_type(prop)
        default = ... if key in required else None
        description = str(prop.get("description", ""))
        fields[key] = (py_type, field_cls(default=default, description=description))
    if not fields:
        return create_model_fn(f"{name.title().replace('_', '')}Input")
    return create_model_fn(f"{name.title().replace('_', '')}Input", **fields)


def _json_schema_type(prop: dict) -> Any:
    prop_type = prop.get("type")
    if prop_type == "string":
        return str
    if prop_type == "integer":
        return int
    if prop_type == "number":
        return float
    if prop_type == "boolean":
        return bool
    if prop_type == "array":
        inner = prop.get("items", {})
        return list[_json_schema_type(inner)]  # type: ignore[index]
    if prop_type == "object":
        return dict[str, Any]
    if "enum" in prop:
        return str
    return Any


# ---------------------------------------------------------------------------
# TaskExecutor
# ---------------------------------------------------------------------------

class TaskExecutor:
    """
    Owns deepagents library loading, channel wiring, and task execution.

    Agent sessions are maintained by the injected DeepAgentsChannel, which
    keeps one persistent agent per (workspace_path, stage, persona) so that
    system-prompt caching and conversation history accumulate across calls.
    """

    def __init__(
        self,
        *,
        workspace_path: str,
        provider_lane: str,
        provider_profile_document: Optional[ProviderProfileDocument],
        tool_registry,
        skill_adapter,
        interrupt_config_fn: Callable[[], dict],
        subagents_fn: Callable[[], list[dict[str, Any]]],
        channel_registry: Optional[ChannelRegistry] = None,
    ) -> None:
        self._workspace_path = workspace_path
        self._provider_lane = provider_lane
        self._provider_profiles = provider_profile_document or default_provider_profile_document(provider_lane)
        self._tool_registry = tool_registry
        self._skill_adapter = skill_adapter
        self._interrupt_config_fn = interrupt_config_fn
        self._subagents_fn = subagents_fn

        self._filesystem_backend_factory: Optional[Callable[..., Any]] = None
        self._structured_tool_cls: Optional[type] = None
        self._field_cls: Optional[type] = None
        self._create_model_fn: Optional[Callable[..., Any]] = None
        self._deepagents_factory: Optional[Callable[..., Any]] = None
        self._deepagents_import_error: Optional[str] = None
        self._load_deepagents()

        # Wire the channel — uses a registry so channels survive across calls.
        self._channel_registry = channel_registry or ChannelRegistry(workspace_path)
        self._channel: Optional[DeepAgentsChannel] = self._channel_registry.get(
            provider_lane,
            provider_profile_document=self._provider_profiles,
            deepagents_factory=self._deepagents_factory,
            filesystem_backend_factory=self._filesystem_backend_factory,
            skill_adapter=skill_adapter,
            build_system_prompt_fn=build_system_prompt,
            build_tools_fn=self._build_langchain_tools,
            subagents_fn=subagents_fn,
            interrupt_config_fn=interrupt_config_fn,
        )

    # ------------------------------------------------------------------
    # AgentRuntime protocol method
    # ------------------------------------------------------------------

    def execute_task(
        self,
        task: Task,
        context: str,
        workspace_path: str = "",
        on_chunk=None,  # noqa: ARG002
        persona: str = "default",
    ) -> list[dict]:
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(persona),
            stage="execution",
            task_type=getattr(task, "task_type", ""),
        )
        prompt = (
            "Return JSON only with shape: "
            '{"edits":[{"targetFile":"","oldContent":"","newContent":"","explanation":""}]}\n\n'
            f"TASK TITLE: {task.title}\n"
            f"TASK DESCRIPTION: {task.description}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="EXECUTION",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
            session_key=task.id or workspace_path,
        )
        if payload is None:
            return []
        return list(payload.get("edits", []))

    # ------------------------------------------------------------------
    # Proxy methods — kept so tests can patch runtime._run_deepagents_*
    # ------------------------------------------------------------------

    def _run_deepagents_structured(
        self,
        *,
        stage: str,
        prompt: str,
        workspace_path: str,
        persona: str,
        session_key: str = "",
    ) -> Optional[dict[str, Any]]:
        response_text = self._run_deepagents_text(
            stage=stage,
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
            session_key=session_key,
        )
        if response_text:
            parsed = _extract_json_object(response_text)
            if isinstance(parsed, dict):
                return parsed
        return None

    def _run_deepagents_text(
        self,
        *,
        stage: str,
        prompt: str,
        workspace_path: str,
        persona: str,
        session_key: str = "",
    ) -> str:
        """
        Execute one agent turn via the persistent DeepAgentsChannel.

        session_key is optional; when provided the channel will maintain a
        persistent conversation thread for that key so that provider-side
        caching accumulates across calls.  Defaults to workspace_path so
        that each workspace has its own implicit session.
        """
        if self._channel is None:
            return ""
        result = self._channel.run(
            stage=stage,
            prompt=prompt,
            persona=persona,
            workspace_path=workspace_path,
            session_key=self._resolve_session_key(
                stage=stage,
                workspace_path=workspace_path,
                persona=persona,
                session_key=session_key,
            ),
        )
        return result.text

    def flush_session(self, session_key: str) -> None:
        if self._channel is not None:
            self._channel.flush(session_key)

    # ------------------------------------------------------------------
    # Internal deepagents library loading
    # ------------------------------------------------------------------

    def _load_deepagents(self) -> None:
        try:
            from deepagents import create_deep_agent
            from deepagents.backends import FilesystemBackend
            from langchain_core.tools import StructuredTool
            from pydantic import Field, create_model

            self._deepagents_factory = create_deep_agent
            self._filesystem_backend_factory = FilesystemBackend
            self._structured_tool_cls = StructuredTool
            self._field_cls = Field
            self._create_model_fn = create_model
            self._deepagents_import_error = None
        except Exception as exc:
            self._deepagents_factory = None
            self._filesystem_backend_factory = None
            self._structured_tool_cls = None
            self._field_cls = None
            self._create_model_fn = None
            self._deepagents_import_error = str(exc)

    def _build_langchain_tools(
        self, workspace_path: str, persona: str, stage: str, bundle: Any
    ) -> list[Any]:
        if not self._structured_tool_cls or not self._create_model_fn or not self._field_cls:
            return []
        tools: list[Any] = []
        descriptors = self._tool_registry.select_descriptors(
            persona=_normalize_persona(persona),
            stage=stage.lower(),
            preferred_categories=getattr(bundle, "preferred_tool_categories", []),
            include_optional=False,
        )
        for descriptor in descriptors:
            if descriptor.policy.optional:
                continue
            args_schema = _schema_to_pydantic_model(
                name=descriptor.name,
                schema=descriptor.input_schema,
                create_model_fn=self._create_model_fn,
                field_cls=self._field_cls,
            )

            def make_runner(name: str):
                def _runner(**kwargs) -> str:
                    try:
                        result = self._tool_registry.invoke(name, kwargs, workspace_path)
                        return json.dumps(result, ensure_ascii=True)
                    except Exception as exc:
                        log.warning("Tool %r raised during invocation: %s", name, exc)
                        return json.dumps({"error": f"Tool {name!r} failed: {exc}"}, ensure_ascii=True)

                _runner.__name__ = name
                return _runner

            tools.append(
                self._structured_tool_cls.from_function(
                    func=make_runner(descriptor.name),
                    name=descriptor.name,
                    description=descriptor.description,
                    args_schema=args_schema,
                )
            )
        return tools

    def _resolve_session_key(
        self,
        *,
        stage: str,
        workspace_path: str,
        persona: str,
        session_key: str,
    ) -> str:
        strategy = self._provider_profiles.policies.session_key_strategy
        stage_key = stage.strip().upper()
        persona_key = persona.strip().lower() or DEFAULT_PERSONA
        base_key = session_key.strip()
        if strategy == "workspace":
            return workspace_path
        if strategy == "workspace_stage":
            return f"{workspace_path}::{stage_key}"
        if strategy == "session_stage_persona_provider":
            return "::".join(part for part in [base_key or workspace_path, stage_key, persona_key, self._provider_lane] if part)
        return "::".join(part for part in [workspace_path, stage_key, persona_key, self._provider_lane] if part)
