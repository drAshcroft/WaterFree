"""OpenAI provider adapter."""
from __future__ import annotations

import hashlib
from typing import Any

from backend.llm.adapters.anthropic_adapter import _extract


class OpenAIAdapter:
    provider_type = "openai"

    def create_llm(self, model_id: str, config: Any) -> Any:
        try:
            from langchain_openai import ChatOpenAI

            model_kwargs = dict(config.get("model_kwargs", {}))
            return ChatOpenAI(
                model=model_id,
                api_key=config.get("api_key"),
                base_url=config.get("base_url"),
                stream_usage=bool(config.get("streamUsage", True)),
                use_responses_api=bool(config.get("useResponsesApi", True)),
                use_previous_response_id=bool(config.get("usePreviousResponseId", True)),
                model_kwargs=model_kwargs,
            )
        except Exception:
            return f"openai:{model_id}"

    def extract_usage(self, response: Any) -> dict[str, int]:
        return _extract(response, use_openai_cache=True)

    def supports_model(self, model_id: str) -> bool:
        from backend.llm.model_catalog import get_model
        m = get_model(model_id)
        return m is not None and m.provider == "openai"

    def supports_capability(self, capability: str) -> bool:
        return capability in {"tools", "vision", "caching", "streaming", "json_mode", "long_context", "reasoning"}

    @staticmethod
    def build_metadata(
        profile: Any,
        stage: str,
        persona: str,
        session_key: str,
    ) -> dict[str, Any]:
        opts = profile.optimizations.openai
        strategy = str(opts.get("promptCacheKeyStrategy", "session_stage_persona")).strip()
        cache_parts = [profile.id]
        if strategy in {"session_stage_persona", "session_stage_persona_provider"}:
            cache_parts.extend([session_key, stage, persona])
        elif strategy == "session":
            cache_parts.append(session_key)
        elif strategy == "stage_persona":
            cache_parts.extend([stage, persona])
        cache_key = hashlib.sha1("::".join(p for p in cache_parts if p).encode()).hexdigest()
        model_kwargs: dict[str, Any] = {"prompt_cache_key": cache_key}
        retention = opts.get("promptCacheRetention")
        if retention:
            model_kwargs["prompt_cache_retention"] = retention
        return {
            "useResponsesApi": opts.get("useResponsesApi", True) is not False,
            "usePreviousResponseId": opts.get("usePreviousResponseId", True) is not False,
            "streamUsage": opts.get("streamUsage", True) is not False,
            "promptCacheKey": cache_key,
            "model_kwargs": model_kwargs,
        }
