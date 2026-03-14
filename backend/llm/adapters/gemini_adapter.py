"""Google Gemini provider adapter.

Caching strategy
────────────────
Gemini supports *implicit* context caching: when the same long prefix is
sent repeatedly the API automatically serves it from cache and reports
``cached_content_token_count`` in the usage metadata.  There is no
explicit "create cache" API call required.  Caching is enabled by passing
``enable_implicit_caching=True`` to ChatGoogleGenerativeAI (default here).

Quirks
──────
* Usage comes back in ``response_metadata.usage_metadata``, not ``usage``
  or ``token_usage``, so we need a custom extractor.
* ``gemini-2.5-pro`` supports extended reasoning (thinking) via
  ``thinking_config`` in model_kwargs, but this is not wired up here yet.
* The ``base_url`` config key is ignored — the SDK always talks directly
  to generativelanguage.googleapis.com.
"""
from __future__ import annotations

from typing import Any


class GeminiAdapter:
    provider_type = "gemini"

    def create_llm(self, model_id: str, config: Any) -> Any:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore[import]

            kwargs: dict[str, Any] = {
                "model": model_id,
                "google_api_key": config.get("api_key"),
            }
            if config.get("enable_implicit_caching", True):
                kwargs["enable_implicit_caching"] = True
            return ChatGoogleGenerativeAI(**kwargs)
        except Exception:
            return f"gemini:{model_id}"

    def extract_usage(self, response: Any) -> dict[str, int]:
        _zero: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
        if not isinstance(response, dict):
            return _zero
        for message in reversed(response.get("messages", [])):
            meta = (
                message.get("response_metadata", {})
                if isinstance(message, dict)
                else getattr(message, "response_metadata", {})
            ) or {}
            usage = meta.get("usage_metadata", {}) or {}
            if not usage:
                continue
            return {
                "input_tokens": int(usage.get("prompt_token_count", 0) or 0),
                "output_tokens": int(usage.get("candidates_token_count", 0) or 0),
                # cached_content_token_count is already part of prompt_token_count
                "cache_read_tokens": int(usage.get("cached_content_token_count", 0) or 0),
                "cache_creation_tokens": 0,
            }
        return _zero

    def supports_model(self, model_id: str) -> bool:
        from backend.llm.model_catalog import get_model
        m = get_model(model_id)
        return m is not None and m.provider == "gemini"

    def supports_capability(self, capability: str) -> bool:
        return capability in {
            "tools", "vision", "reasoning", "caching", "streaming", "json_mode", "long_context",
        }
