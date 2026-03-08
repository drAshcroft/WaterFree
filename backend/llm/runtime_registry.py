"""
Runtime registry/factory for selecting an LLM runtime implementation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from backend.graph.client import GraphClient
from backend.knowledge.store import KnowledgeStore
from backend.llm.providers import (
    AnthropicRuntime,
    DeepAgentsRuntime,
    OllamaRuntime,
    OpenAIRuntime,
)
from backend.llm.runtime import AgentRuntime
from backend.todo.store import TaskStore

_DEFAULT_RUNTIME = "anthropic"
_RUNTIME_ENV_VAR = "WATERFREE_AGENT_RUNTIME"


@dataclass(frozen=True)
class RuntimeDescriptor:
    id: str
    label: str
    provider: str
    local: bool
    supports_tools: bool
    supports_skills: bool
    supports_checkpoints: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "local": self.local,
            "supportsTools": self.supports_tools,
            "supportsSkills": self.supports_skills,
            "supportsCheckpoints": self.supports_checkpoints,
        }


_RUNTIME_DESCRIPTORS: dict[str, RuntimeDescriptor] = {
    "anthropic": RuntimeDescriptor(
        id="anthropic",
        label="Anthropic Claude",
        provider="anthropic",
        local=False,
        supports_tools=True,
        supports_skills=True,
        supports_checkpoints=True,
    ),
    "deep_agents": RuntimeDescriptor(
        id="deep_agents",
        label="Deep Agents",
        provider="deep_agents",
        local=False,
        supports_tools=True,
        supports_skills=True,
        supports_checkpoints=True,
    ),
    "ollama": RuntimeDescriptor(
        id="ollama",
        label="Ollama (Local Lane)",
        provider="ollama",
        local=True,
        supports_tools=True,
        supports_skills=True,
        supports_checkpoints=True,
    ),
    "openai": RuntimeDescriptor(
        id="openai",
        label="OpenAI",
        provider="openai",
        local=False,
        supports_tools=True,
        supports_skills=True,
        supports_checkpoints=True,
    ),
}


def resolve_runtime_name(preferred: Optional[str] = None) -> str:
    raw = (preferred or os.environ.get(_RUNTIME_ENV_VAR) or _DEFAULT_RUNTIME).strip().lower()
    if raw in {"anthropic", "claude"}:
        return "anthropic"
    if raw in {"deep-agents", "deep_agents"}:
        return "deep_agents"
    if raw == "ollama":
        return "ollama"
    if raw == "openai":
        return "openai"
    raise ValueError(f"Unsupported runtime '{raw}'.")


def list_runtime_descriptors() -> list[RuntimeDescriptor]:
    return [descriptor for _, descriptor in sorted(_RUNTIME_DESCRIPTORS.items())]


def choose_runtime_for_stage(
    *,
    stage: str,
    workload: str = "",
    preferred: Optional[str] = None,
) -> str:
    if preferred:
        return resolve_runtime_name(preferred)

    stage_key = stage.strip().lower()
    workload_key = workload.strip().lower()
    if "knowledge" in workload_key or stage_key in {"snippetize", "knowledge"}:
        return "ollama"
    if stage_key in {"planning", "annotation", "execution", "debug"}:
        return "deep_agents"
    return resolve_runtime_name()


def create_runtime(
    *,
    runtime_name: Optional[str] = None,
    graph: Optional[GraphClient] = None,
    knowledge_store: Optional[KnowledgeStore] = None,
    task_store_factory: Optional[Callable[[str], TaskStore]] = None,
    workspace_path: str = ".",
) -> AgentRuntime:
    name = resolve_runtime_name(runtime_name)
    common_kwargs = {
        "graph": graph,
        "knowledge_store": knowledge_store,
        "task_store_factory": task_store_factory,
    }
    if name == "anthropic":
        return AnthropicRuntime(**common_kwargs)
    if name == "deep_agents":
        return DeepAgentsRuntime(workspace_path=workspace_path, **common_kwargs)
    if name == "ollama":
        return OllamaRuntime(**common_kwargs)
    if name == "openai":
        return OpenAIRuntime(**common_kwargs)
    raise ValueError(f"Unsupported runtime '{name}'.")
