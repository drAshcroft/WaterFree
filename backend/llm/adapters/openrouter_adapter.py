"""OpenRouter provider adapter.

OpenRouter is an OpenAI-compatible gateway in front of many vendors, so it
reuses the OpenAI wire format — but deliberately NOT the OpenAI adapter:

  * OpenRouter serves ``/chat/completions`` only. ``OpenAIAdapter`` defaults
    ``use_responses_api`` to True, which would send the wrong endpoint.
  * ``prompt_cache_key`` / ``prompt_cache_retention`` are OpenAI Responses-API
    parameters. OpenRouter rejects unknown top-level fields, so the whole
    prompt-cache metadata block is intentionally absent here.

Model ids are ``vendor/model`` (e.g. ``anthropic/claude-sonnet-4.5``) and the
catalog is far too large to enumerate, so :meth:`supports_model` validates the
shape rather than looking the id up in ``model_catalog``.
"""
from __future__ import annotations

import os
from typing import Any

from backend.llm.adapters.anthropic_adapter import _extract
from backend.llm.provider_profiles import OPENROUTER_API_KEY_ENV, OPENROUTER_BASE_URL

# Optional attribution headers. OpenRouter surfaces these on its dashboard; they
# are not required for auth and carry no user data.
_REFERER = "https://github.com/drAshcroft/WaterFree"
_TITLE = "WaterFree"


class OpenRouterAdapter:
    provider_type = "openrouter"

    def create_llm(self, model_id: str, config: Any) -> Any:
        try:
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model_id,
                api_key=resolve_api_key(config.get("api_key")),
                base_url=config.get("base_url") or OPENROUTER_BASE_URL,
                stream_usage=bool(config.get("streamUsage", True)),
                # Chat Completions only — see module docstring.
                use_responses_api=False,
                default_headers={"HTTP-Referer": _REFERER, "X-Title": _TITLE},
                model_kwargs=dict(config.get("model_kwargs", {})),
            )
        except Exception:
            return f"openrouter:{model_id}"

    def extract_usage(self, response: Any) -> dict[str, int]:
        # OpenRouter normalizes upstream usage into the OpenAI shape, including
        # prompt_tokens_details.cached_tokens for vendors that report caching.
        return _extract(response, use_openai_cache=True)

    def supports_model(self, model_id: str) -> bool:
        return is_openrouter_model_id(model_id)

    def supports_capability(self, capability: str) -> bool:
        # Per-model reality varies across the gateway's catalog; this advertises
        # what OpenRouter itself can pass through.
        return capability in {
            "tools", "vision", "caching", "streaming",
            "json_mode", "long_context", "reasoning",
        }


def is_openrouter_model_id(model_id: str) -> bool:
    """True for a ``vendor/model`` id with both halves non-empty."""
    value = str(model_id or "").strip()
    if value.count("/") != 1:
        return False
    vendor, _, name = value.partition("/")
    return bool(vendor.strip()) and bool(name.strip())


def resolve_api_key(profile_key: Any) -> str:
    """Profile key first, then the environment.

    The profile only carries a key when the extension host exported it from VS
    Code SecretStorage. The standalone `waterfree` CLI has no SecretStorage, so
    it falls back to $OPENROUTER_API_KEY.
    """
    key = str(profile_key or "").strip()
    if key:
        return key
    return os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
