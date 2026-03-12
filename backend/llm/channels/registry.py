"""
Channel registry — maps a provider_lane to the right ProviderChannel.

DeepAgents-capable lanes (deep_agents, anthropic, openai, ollama) get a
DeepAgentsChannel.  Fallback lanes (huggingface, mock, monitor) keep using
ContextLifecycleManager directly and are not wrapped here.

One channel instance is kept per (provider_lane, workspace_path) pair.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from backend.llm.channels.deepagents_channel import DeepAgentsChannel
from backend.llm.channels.usage import UsageStore
from backend.llm.provider_profiles import ProviderProfileDocument

log = logging.getLogger(__name__)

# Lanes that support the DeepAgents channel.
_DEEPAGENTS_LANES = {"deep_agents", "anthropic", "openai", "ollama"}


class ChannelRegistry:
    """
    Factory and cache for ProviderChannel instances.

    Usage:
        registry = ChannelRegistry(workspace_path=workspace_path)
        channel = registry.get(provider_lane, **deps)

    The same channel instance is returned for the same (lane, workspace_path)
    so agent sessions survive across multiple handler calls.
    """

    def __init__(self, workspace_path: str) -> None:
        self._workspace_path = workspace_path
        self._usage_store = UsageStore(workspace_path)
        self._channels: dict[str, DeepAgentsChannel] = {}

    def get(
        self,
        provider_lane: str,
        *,
        provider_profile_document: ProviderProfileDocument,
        deepagents_factory: Optional[Callable[..., Any]],
        filesystem_backend_factory: Optional[Callable[..., Any]],
        skill_adapter: Any,
        build_system_prompt_fn: Callable[[str, str], str],
        build_tools_fn: Callable[[str, str, str, Any], list],
        subagents_fn: Callable[[], list[dict]],
        interrupt_config_fn: Callable[[], dict],
    ) -> Optional[DeepAgentsChannel]:
        """
        Return the DeepAgentsChannel for this lane, creating it on first call.
        Returns None if the lane is not DeepAgents-capable or the factory is
        unavailable.
        """
        if provider_lane not in _DEEPAGENTS_LANES:
            return None
        if deepagents_factory is None:
            return None

        cache_key = f"{provider_lane}::{provider_profile_document.profile_hash}"
        if cache_key not in self._channels:
            self._channels[cache_key] = DeepAgentsChannel(
                provider_lane=provider_lane,
                provider_profile_document=provider_profile_document,
                deepagents_factory=deepagents_factory,
                filesystem_backend_factory=filesystem_backend_factory,
                skill_adapter=skill_adapter,
                build_system_prompt_fn=build_system_prompt_fn,
                build_tools_fn=build_tools_fn,
                subagents_fn=subagents_fn,
                interrupt_config_fn=interrupt_config_fn,
                usage_store=self._usage_store,
            )
        return self._channels[cache_key]

    def get_usage_stats(self) -> list[dict]:
        """Return all persisted usage records for this workspace."""
        return self._usage_store.get_all()
