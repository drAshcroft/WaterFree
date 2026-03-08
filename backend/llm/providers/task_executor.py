"""
Task execution engine for the Deep Agents runtime.

Owns: deepagents library loading, agent creation, tool-loop invocation,
structured/text response extraction, and the execute_task protocol method.
Also houses shared module-level helpers used across the providers package.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Optional

from backend.llm.prompt_templates import build_system_prompt
from backend.llm.personas import DEFAULT_PERSONA, PERSONAS
from backend.llm.tools import build_default_tool_registry
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


def _model_name_for_lane(provider_lane: str) -> str:
    lane = provider_lane.strip().lower()
    if lane == "openai":
        return os.environ.get("WATERFREE_OPENAI_MODEL", "openai:o3-mini")
    if lane == "ollama":
        return os.environ.get("WATERFREE_OLLAMA_MODEL", "ollama:qwen2.5-coder:14b")
    if lane == "anthropic":
        return os.environ.get("WATERFREE_ANTHROPIC_MODEL", "anthropic:claude-sonnet-4-20250514")
    return os.environ.get("WATERFREE_DEEPAGENTS_MODEL", "anthropic:claude-sonnet-4-20250514")


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
        if key in required:
            default = ...
        else:
            default = None
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
    """Owns deepagents library loading, agent creation, and task execution."""

    def __init__(
        self,
        *,
        workspace_path: str,
        provider_lane: str,
        tool_registry,
        skill_adapter,
        interrupt_config_fn: Callable[[], dict],
        subagents_fn: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self._workspace_path = workspace_path
        self._provider_lane = provider_lane
        self._tool_registry = tool_registry
        self._skill_adapter = skill_adapter
        self._interrupt_config_fn = interrupt_config_fn
        self._subagents_fn = subagents_fn

        self._deepagents_factory: Optional[Callable[..., Any]] = None
        self._filesystem_backend_factory: Optional[Callable[..., Any]] = None
        self._structured_tool_cls: Optional[type] = None
        self._field_cls: Optional[type] = None
        self._create_model_fn: Optional[Callable[..., Any]] = None
        self._deepagents_import_error: Optional[str] = None
        self._load_deepagents()

    # ------------------------------------------------------------------
    # AgentRuntime protocol method
    # ------------------------------------------------------------------

    def execute_task(
        self,
        task: Task,
        context: str,
        workspace_path: str = "",
        on_chunk=None,  # noqa: ARG002 — streaming not used by this runtime
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
        )
        if payload is None:
            return []
        return list(payload.get("edits", []))

    # ------------------------------------------------------------------
    # Internal deepagents plumbing
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

    def _run_deepagents_structured(
        self,
        *,
        stage: str,
        prompt: str,
        workspace_path: str,
        persona: str,
    ) -> Optional[dict[str, Any]]:
        response_text = self._run_deepagents_text(
            stage=stage,
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
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
    ) -> str:
        if not self._deepagents_factory:
            return ""
        agent = self._create_agent(stage=stage, workspace_path=workspace_path, persona=persona)
        if agent is None:
            return ""
        try:
            result = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
            return _extract_response_text(result)
        except Exception:
            return ""

    def _create_agent(self, *, stage: str, workspace_path: str, persona: str):
        if not self._deepagents_factory:
            return None
        system_prompt = build_system_prompt(stage.upper(), persona)
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage=stage.lower())
        system_prompt = self._skill_adapter.augment_system_prompt(system_prompt, bundle)
        tools = self._build_langchain_tools(workspace_path=workspace_path)
        model_name = _model_name_for_lane(self._provider_lane)
        kwargs: dict[str, Any] = {
            "model": model_name,
            "tools": tools,
            "system_prompt": system_prompt,
            "subagents": self._subagents_fn(),
            "interrupt_on": self._interrupt_config_fn(),
        }
        if self._filesystem_backend_factory:
            kwargs["backend"] = self._filesystem_backend_factory(root_dir=workspace_path or self._workspace_path)
        return self._deepagents_factory(**kwargs)

    def _build_langchain_tools(self, *, workspace_path: str) -> list[Any]:
        if not self._structured_tool_cls or not self._create_model_fn or not self._field_cls:
            return []
        tools: list[Any] = []
        for descriptor in self._tool_registry.list_descriptors(include_optional=False):
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
                    result = self._tool_registry.invoke(name, kwargs, workspace_path)
                    return json.dumps(result, ensure_ascii=True)

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
