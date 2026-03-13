"""
DeepAgents provider channel.

One persistent agent instance is kept alive per exact provider-aware session key
so that conversation continuity and provider-specific request shaping can be
reused safely across tasks, stages, and personas.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from backend.llm.channels.base import ChannelResult, UsageStats
from backend.llm.channels.usage import UsageStore
from backend.llm.provider_factory import build_runtime_spec, extract_usage
from backend.llm.provider_profiles import ProviderProfileDocument
from backend.llm.provider_resolver import resolve_provider

log = logging.getLogger(__name__)


def _extract_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for message in reversed(result.get("messages", [])):
            content = (
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", "")
            )
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                for part in reversed(content):
                    text = (
                        part.get("text")
                        if isinstance(part, dict)
                        else getattr(part, "text", "")
                    )
                    if isinstance(text, str) and text.strip():
                        return text
    return str(result)


class DeepAgentsChannel:
    """Provider-aware channel backed by the deepagents library."""

    def __init__(
        self,
        *,
        provider_lane: str,
        provider_profile_document: ProviderProfileDocument,
        deepagents_factory: Callable[..., Any],
        filesystem_backend_factory: Optional[Callable[..., Any]],
        skill_adapter: Any,
        build_system_prompt_fn: Callable[[str, str], str],
        build_tools_fn: Callable[[str, str, str, Any], list],
        subagents_fn: Callable[[], list[dict]],
        interrupt_config_fn: Callable[[], dict],
        usage_store: Optional[UsageStore] = None,
    ) -> None:
        self._lane = provider_lane
        self._provider_profiles = provider_profile_document
        self._da_factory = deepagents_factory
        self._fs_backend_factory = filesystem_backend_factory
        self._skill_adapter = skill_adapter
        self._build_system_prompt = build_system_prompt_fn
        self._build_tools = build_tools_fn
        self._subagents_fn = subagents_fn
        self._interrupt_config_fn = interrupt_config_fn
        self._usage_store = usage_store
        self._agents: dict[str, Any] = {}
        self._agent_usage_keys: dict[str, str] = {}
        self._total_usage = UsageStats()

    def run(
        self,
        *,
        stage: str,
        prompt: str,
        persona: str,
        workspace_path: str,
        session_key: str = "",
    ) -> ChannelResult:
        resolved = resolve_provider(
            self._provider_profiles,
            stage=stage,
            persona=persona,
            preferred_runtime=self._lane,
        )
        if resolved is None:
            log.warning("No provider available for lane=%s stage=%s persona=%s", self._lane, stage, persona)
            return ChannelResult(text="", usage=UsageStats())

        key = self._agent_key(
            workspace_path=workspace_path,
            stage=stage,
            persona=persona,
            session_key=session_key,
            provider_id=resolved.profile.id,
        )
        agent = self._agents.get(key)
        if agent is None:
            agent, usage_key = self._create_agent(
                stage=stage,
                persona=persona,
                workspace_path=workspace_path,
                session_key=session_key or workspace_path,
                provider_profile=resolved.profile,
                runtime_name=resolved.runtime_name,
            )
            if agent is None:
                return ChannelResult(text="", usage=UsageStats())
            self._agents[key] = agent
            self._agent_usage_keys[key] = usage_key

        config: dict[str, Any] = {"configurable": {"thread_id": key}} if session_key else {}
        try:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config,
            )
        except Exception as exc:
            log.warning(
                "DeepAgentsChannel.run failed (lane=%s provider=%s stage=%s): %s",
                self._lane,
                resolved.profile.id,
                stage,
                exc,
            )
            return ChannelResult(text="", usage=UsageStats())

        text = _extract_text(result)
        usage = self._extract_usage(result, resolved.profile.provider_kind())
        self._total_usage = self._total_usage + usage
        log.info(
            "LLM call complete: lane=%s provider=%s stage=%s model=%s "
            "in=%d out=%d cache_create=%d cache_read=%d hit_rate=%.1f%%",
            self._lane,
            resolved.profile.id,
            stage,
            resolved.profile.model_for_stage(stage),
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_tokens,
            usage.cache_read_tokens,
            usage.cache_hit_rate * 100,
        )
        if self._usage_store:
            usage_key = self._agent_usage_keys.get(key, resolved.profile.id)
            self._usage_store.record(
                usage_key,
                usage,
                provider_type=resolved.profile.provider_kind(),
                model=resolved.profile.model_for_stage(stage),
                persona=persona,
                stage=stage,
            )
        return ChannelResult(text=text, usage=usage)

    def flush(self, session_key: str) -> None:
        """Drop all agents for the exact session key prefix."""
        prefix = f"{session_key}::"
        to_drop = [key for key in self._agents if key.startswith(prefix)]
        for key in to_drop:
            del self._agents[key]
            self._agent_usage_keys.pop(key, None)

    def get_cumulative_usage(self) -> dict:
        return {"provider": self._lane, **self._total_usage.to_dict()}

    def _agent_key(
        self,
        *,
        workspace_path: str,
        stage: str,
        persona: str,
        session_key: str,
        provider_id: str,
    ) -> str:
        base = "::".join([
            workspace_path,
            provider_id,
            stage.upper(),
            persona.strip().lower() or "default",
        ])
        return f"{session_key}::{base}" if session_key else base

    def _create_agent(
        self,
        *,
        stage: str,
        persona: str,
        workspace_path: str,
        session_key: str,
        provider_profile,
        runtime_name: str,
    ) -> tuple[Optional[Any], str]:
        from backend.llm.personas import DEFAULT_PERSONA, PERSONAS

        norm_persona = persona.strip().lower()
        if norm_persona not in PERSONAS:
            norm_persona = DEFAULT_PERSONA

        bundle = self._skill_adapter.select(persona=norm_persona, stage=stage.lower())
        system_prompt = self._build_system_prompt(stage.upper(), persona)
        system_prompt = self._skill_adapter.augment_system_prompt(system_prompt, bundle)
        tools = self._build_tools(workspace_path, persona, stage, bundle)
        spec = build_runtime_spec(
            provider_profile,
            runtime_name=runtime_name,
            stage=stage,
            persona=norm_persona,
            session_key=session_key,
            policies=self._provider_profiles.policies,
        )

        kwargs: dict[str, Any] = {
            "model": spec.model,
            "tools": tools,
            "system_prompt": system_prompt,
            "subagents": self._subagents_fn(),
            "interrupt_on": self._interrupt_config_fn(),
            "name": spec.provider_id,
        }
        if self._fs_backend_factory:
            kwargs["backend"] = self._fs_backend_factory(root_dir=workspace_path or ".")
        self._attach_middleware(kwargs, stage=stage, spec=spec)

        try:
            return self._da_factory(**kwargs), spec.usage_key
        except Exception as exc:
            log.warning(
                "Failed to create agent (lane=%s provider=%s stage=%s): %s",
                self._lane,
                provider_profile.id,
                stage,
                exc,
            )
            return None, spec.usage_key

    def _attach_middleware(self, kwargs: dict[str, Any], *, stage: str, spec) -> None:
        middlewares: list[Any] = []
        if spec.supports_prompt_caching_middleware:
            try:
                from deepagents.middleware import AnthropicPromptCachingMiddleware  # type: ignore[import]

                middlewares.append(AnthropicPromptCachingMiddleware())
                log.debug("AnthropicPromptCachingMiddleware attached for stage=%s", stage)
            except Exception as exc:
                log.warning(
                    "AnthropicPromptCachingMiddleware unavailable — prompt caching disabled for %s/%s: %s",
                    self._lane,
                    stage,
                    exc,
                )

        if spec.summarization_enabled:
            try:
                from deepagents.middleware import SummarizationMiddleware  # type: ignore[import]

                backend = kwargs.get("backend")
                if backend is not None:
                    middlewares.append(
                        SummarizationMiddleware(
                            model=spec.model,
                            backend=backend,
                            trigger=("tokens", spec.summarization_threshold),
                        )
                    )
            except Exception as exc:
                log.debug("Summarization middleware unavailable for %s/%s: %s", self._lane, stage, exc)

        if middlewares:
            kwargs["middleware"] = middlewares

    def _extract_usage(self, result: Any, provider_type: str) -> UsageStats:
        usage = extract_usage(result, provider_type)
        return UsageStats(
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_tokens"],
            cache_creation_tokens=usage["cache_creation_tokens"],
        )
