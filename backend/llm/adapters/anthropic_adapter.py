"""Anthropic/Claude provider adapter."""
from __future__ import annotations

from typing import Any


class AnthropicAdapter:
    provider_type = "anthropic"

    def create_llm(self, model_id: str, config: Any) -> Any:
        extended_thinking = bool(config.get("extended_thinking_enabled", False))
        thinking_budget = int(config.get("thinking_budget_tokens", 10_000))
        try:
            from langchain_anthropic import ChatAnthropic

            model_kwargs: dict[str, Any] = {}
            if extended_thinking:
                model_kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": thinking_budget,
                }
            return ChatAnthropic(
                model_name=model_id,
                api_key=config.get("api_key"),
                base_url=config.get("base_url"),
                stream_usage=True,
                **({"model_kwargs": model_kwargs} if model_kwargs else {}),
            )
        except Exception:
            return f"anthropic:{model_id}"

    def extract_usage(self, response: Any) -> dict[str, int]:
        return _extract(response, use_openai_cache=False)

    def supports_model(self, model_id: str) -> bool:
        from backend.llm.model_catalog import get_model
        m = get_model(model_id)
        return m is not None and m.provider == "claude"

    def supports_capability(self, capability: str) -> bool:
        return capability in {"tools", "vision", "caching", "reasoning", "streaming", "json_mode", "long_context"}


def _extract(response: Any, *, use_openai_cache: bool) -> dict[str, int]:
    _zero: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}
    if not isinstance(response, dict):
        return _zero
    for message in reversed(response.get("messages", [])):
        meta = (
            message.get("response_metadata", {})
            if isinstance(message, dict)
            else getattr(message, "response_metadata", {})
        ) or {}
        usage = meta.get("usage", meta.get("token_usage", {})) or {}
        if not usage:
            continue
        prompt_details = usage.get("prompt_tokens_details", {}) or {}
        if use_openai_cache:
            cache_read = int(prompt_details.get("cached_tokens", 0) or 0)
            cache_creation = 0
        else:
            cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
            cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
        return {
            "input_tokens": int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
            "output_tokens": int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
        }
    return _zero
