"""
Runtime registry/factory for selecting an LLM runtime implementation.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from backend.graph.client import GraphClient
from backend.knowledge.store import KnowledgeStore
from backend.llm.providers import AnthropicRuntime
from backend.llm.runtime import AgentRuntime
from backend.todo.store import TaskStore

_DEFAULT_RUNTIME = "anthropic"
_RUNTIME_ENV_VAR = "WATERFREE_AGENT_RUNTIME"


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


def create_runtime(
    *,
    runtime_name: Optional[str] = None,
    graph: Optional[GraphClient] = None,
    knowledge_store: Optional[KnowledgeStore] = None,
    task_store_factory: Optional[Callable[[str], TaskStore]] = None,
) -> AgentRuntime:
    name = resolve_runtime_name(runtime_name)
    if name == "anthropic":
        return AnthropicRuntime(
            graph=graph,
            knowledge_store=knowledge_store,
            task_store_factory=task_store_factory,
        )
    raise NotImplementedError(
        f"Runtime '{name}' is declared but not implemented yet. Set {_RUNTIME_ENV_VAR}=anthropic."
    )
