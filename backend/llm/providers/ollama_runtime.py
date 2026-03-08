"""
Ollama lane runtime.

Current implementation keeps behavior parity by delegating execution to the
shared Anthropic runtime while exposing workload routing metadata for local
model lanes.
"""

from __future__ import annotations

import os

from .anthropic_runtime import AnthropicRuntime


class OllamaRuntime(AnthropicRuntime):
    @property
    def runtime_id(self) -> str:
        return "ollama"

    @property
    def preferred_workloads(self) -> list[str]:
        return [
            "snippet triage",
            "procedure summarization drafts",
            "tag generation",
            "local embeddings",
            "reranking",
            "repo-scale knowledge extraction",
        ]

    def local_model_name(self) -> str:
        return os.environ.get("WATERFREE_OLLAMA_MODEL", "qwen2.5-coder:14b")
