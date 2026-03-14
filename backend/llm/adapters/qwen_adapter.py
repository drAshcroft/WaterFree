"""Alibaba Qwen/Dashscope provider adapter.

Caching strategy
────────────────
Qwen is accessed via Dashscope's OpenAI-compatible endpoint.  Dashscope
does not expose explicit prompt caching controls through this interface;
any server-side prefix caching is automatic and transparent.  Usage
extraction mirrors the OpenAI path (``prompt_tokens_details.cached_tokens``),
so cache hit rates will appear in the UI if Dashscope starts reporting them.

Quirks
──────
* Default base URL is the Dashscope OpenAI-compatible endpoint.  A custom
  ``base_url`` in the provider profile will override it (useful for
  proxies or on-prem deployments).
* Tool calling is supported on qwen-max and qwen-plus; qwen-turbo has
  limited function-calling reliability — use with care.
* ``stream_usage=True`` is required to get token counts back in streaming
  mode; the Dashscope endpoint honours this flag just like the OpenAI one.
"""
from __future__ import annotations

from typing import Any

from backend.llm.adapters.anthropic_adapter import _extract

_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class QwenAdapter:
    provider_type = "qwen"

    def create_llm(self, model_id: str, config: Any) -> Any:
        try:
            from langchain_openai import ChatOpenAI  # type: ignore[import]

            return ChatOpenAI(
                model=model_id,
                api_key=config.get("api_key"),
                base_url=config.get("base_url") or _DASHSCOPE_BASE_URL,
                stream_usage=bool(config.get("streamUsage", True)),
            )
        except Exception:
            return f"qwen:{model_id}"

    def extract_usage(self, response: Any) -> dict[str, int]:
        return _extract(response, use_openai_cache=True)

    def supports_model(self, model_id: str) -> bool:
        from backend.llm.model_catalog import get_model
        m = get_model(model_id)
        return m is not None and m.provider == "qwen"

    def supports_capability(self, capability: str) -> bool:
        return capability in {"tools", "vision", "streaming", "json_mode", "long_context"}
