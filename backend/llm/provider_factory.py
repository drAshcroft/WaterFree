"""Provider-specific model construction and usage extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.llm.adapters import get_adapter
from backend.llm.adapters.openai_adapter import OpenAIAdapter
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
    extended_thinking_enabled: bool = False
    thinking_budget_tokens: int = 10_000
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
    model_name_override: str = "",
) -> ProviderRuntimeSpec:
    stage_key = normalize_stage_name(stage)
    model_name = model_name_override.strip() or profile.model_for_stage(stage_key)
    provider_type = profile.provider_kind()
    threshold = policies.summarization_thresholds.get(stage.upper(), 30_000)
    metadata: dict[str, Any] = {}
    model: Any = f"{provider_type}:{model_name}" if model_name else profile.type
    extended_thinking = False
    thinking_budget = 0

    adapter = get_adapter(provider_type)
    if adapter is not None:
        config: dict[str, Any] = {
            "api_key": profile.connection.api_key or None,
            "base_url": profile.connection.base_url or None,
        }
        if provider_type == "anthropic":
            anthropic_opts = profile.optimizations.anthropic
            thinking_budget = int(anthropic_opts.get("thinkingBudgetTokens", 10_000))
            # Extended thinking only applies to reasoning-heavy stages.
            _thinking_stages = {"planning", "annotation", "question_answer", "alter_annotation"}
            extended_thinking = (
                bool(anthropic_opts.get("extendedThinking", False))
                and stage_key in _thinking_stages
            )
            config["extended_thinking_enabled"] = extended_thinking
            config["thinking_budget_tokens"] = thinking_budget
        elif provider_type == "openai":
            metadata = OpenAIAdapter.build_metadata(profile, stage_key, persona, session_key)
            config.update(metadata)
        elif provider_type == "gemini":
            gemini_opts = profile.optimizations.gemini
            config["enable_implicit_caching"] = bool(gemini_opts.get("enableImplicitCaching", True))
        model = adapter.create_llm(model_name, config)
        if provider_type == "anthropic" and isinstance(model, str):
            # create_llm fell back to string — disable extended thinking
            extended_thinking = False

    return ProviderRuntimeSpec(
        provider_id=profile.id,
        provider_type=provider_type,
        provider_label=profile.label,
        runtime_name=runtime_name,
        model_name=model_name,
        model=model,
        # Prompt caching and extended thinking are mutually exclusive for Anthropic.
        supports_prompt_caching_middleware=provider_type == "anthropic"
        and bool(profile.optimizations.anthropic.get("enablePromptCaching", True))
        and not extended_thinking,
        extended_thinking_enabled=extended_thinking,
        thinking_budget_tokens=thinking_budget,
        summarization_enabled=profile.features.summarization,
        summarization_threshold=threshold,
        usage_key=profile.id,
        metadata=metadata,
    )


def extract_usage(result: Any, provider_type: str) -> dict[str, int]:
    adapter = get_adapter(provider_type)
    if adapter is not None:
        return adapter.extract_usage(result)
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}


def normalize_stage_name(stage: str) -> str:
    value = stage.strip().lower()
    return {
        "live_debug": "debug",
        "question_answer": "question_answer",
        "ripple_detection": "ripple_detection",
        "alter_annotation": "alter_annotation",
    }.get(value, value)
