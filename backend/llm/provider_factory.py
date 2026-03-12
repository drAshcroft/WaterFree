"""Provider-specific model construction and usage extraction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from backend.llm.provider_profiles import ProviderPolicies, ProviderProfile


@dataclass(frozen=True)
class ProviderRuntimeSpec:
    provider_id: str
    provider_type: str
    provider_label: str
    runtime_name: str
    model_name: str
    model: Any
    supports_prompt_caching_middleware: bool = False
    summarization_enabled: bool = True
    summarization_threshold: int = 30_000
    usage_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def build_runtime_spec(
    profile: ProviderProfile,
    *,
    runtime_name: str,
    stage: str,
    persona: str,
    session_key: str,
    policies: ProviderPolicies,
) -> ProviderRuntimeSpec:
    stage_key = normalize_stage_name(stage)
    model_name = profile.model_for_stage(stage_key)
    provider_type = profile.provider_kind()
    threshold = policies.summarization_thresholds.get(stage.upper(), 30_000)
    metadata: dict[str, Any] = {}
    model: Any = f"{provider_type}:{model_name}" if model_name else profile.type

    if provider_type == "openai":
        metadata = build_openai_metadata(profile, stage_key, persona, session_key)
        try:
            from langchain_openai import ChatOpenAI

            model_kwargs = dict(metadata.get("model_kwargs", {}))
            model = ChatOpenAI(
                model=model_name,
                api_key=profile.connection.api_key or None,
                base_url=profile.connection.base_url or None,
                stream_usage=bool(metadata.get("streamUsage", True)),
                use_responses_api=bool(metadata.get("useResponsesApi", True)),
                use_previous_response_id=bool(metadata.get("usePreviousResponseId", True)),
                model_kwargs=model_kwargs,
            )
        except Exception:
            model = f"openai:{model_name}"
    elif provider_type == "anthropic":
        try:
            from langchain_anthropic import ChatAnthropic

            model = ChatAnthropic(
                model_name=model_name,
                api_key=profile.connection.api_key or None,
                base_url=profile.connection.base_url or None,
                stream_usage=True,
            )
        except Exception:
            model = f"anthropic:{model_name}"
    elif provider_type == "ollama":
        model = f"ollama:{model_name}"

    return ProviderRuntimeSpec(
        provider_id=profile.id,
        provider_type=provider_type,
        provider_label=profile.label,
        runtime_name=runtime_name,
        model_name=model_name,
        model=model,
        supports_prompt_caching_middleware=provider_type == "anthropic"
        and bool(profile.optimizations.anthropic.get("enablePromptCaching", True)),
        summarization_enabled=profile.features.summarization,
        summarization_threshold=threshold,
        usage_key=profile.id,
        metadata=metadata,
    )


def build_openai_metadata(
    profile: ProviderProfile,
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
    cache_key = hashlib.sha1("::".join(part for part in cache_parts if part).encode("utf-8")).hexdigest()
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


def extract_usage(result: Any, provider_type: str) -> dict[str, int]:
    if not isinstance(result, dict):
        return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}
    for message in reversed(result.get("messages", [])):
        meta = (
            message.get("response_metadata", {})
            if isinstance(message, dict)
            else getattr(message, "response_metadata", {})
        ) or {}
        usage = meta.get("usage", meta.get("token_usage", {})) or {}
        if not usage:
            continue
        prompt_details = usage.get("prompt_tokens_details", {}) or {}
        cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
        cache_creation_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
        if provider_type == "openai":
            cache_read_tokens = int(prompt_details.get("cached_tokens", 0) or 0)
            cache_creation_tokens = 0
        return {
            "input_tokens": int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
            "output_tokens": int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
            "cache_read_tokens": cache_read_tokens,
            "cache_creation_tokens": cache_creation_tokens,
        }
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}


def normalize_stage_name(stage: str) -> str:
    value = stage.strip().lower()
    return {
        "live_debug": "debug",
        "question_answer": "question_answer",
        "ripple_detection": "ripple_detection",
        "alter_annotation": "alter_annotation",
    }.get(value, value)
