"""Groq provider adapter."""
from __future__ import annotations

from typing import Any

from backend.llm.adapters.anthropic_adapter import _extract


class GroqAdapter:
    provider_type = "groq"

    def create_llm(self, model_id: str, config: Any) -> Any:
        try:
            from langchain_groq import ChatGroq

            return ChatGroq(
                model=model_id,
                api_key=config.get("api_key"),
            )
        except Exception:
            return f"groq:{model_id}"

    def extract_usage(self, response: Any) -> dict[str, int]:
        return _extract(response, use_openai_cache=True)

    def supports_model(self, model_id: str) -> bool:
        from backend.llm.model_catalog import get_model
        m = get_model(model_id)
        return m is not None and m.provider == "groq"

    def supports_capability(self, capability: str) -> bool:
        return capability in {"tools", "streaming", "json_mode"}
