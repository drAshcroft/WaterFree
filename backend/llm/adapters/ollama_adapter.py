"""Ollama provider adapter."""
from __future__ import annotations

from typing import Any


class OllamaAdapter:
    provider_type = "ollama"

    def create_llm(self, model_id: str, config: Any) -> Any:
        return f"ollama:{model_id}"

    def extract_usage(self, response: Any) -> dict[str, int]:
        return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}

    def supports_model(self, model_id: str) -> bool:
        from backend.llm.model_catalog import get_model
        m = get_model(model_id)
        return m is not None and m.provider == "ollama"

    def supports_capability(self, capability: str) -> bool:
        return capability in {"tools", "streaming"}
