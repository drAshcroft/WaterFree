"""Provider profile schema, normalization, and workspace loading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PROFILE_PATH = (".waterfree", "providers.json")

DEFAULT_SUMMARIZATION_THRESHOLDS: dict[str, int] = {
    "EXECUTION": 60_000,
    "PLANNING": 30_000,
    "ANNOTATION": 30_000,
    "ALTER_ANNOTATION": 30_000,
    "LIVE_DEBUG": 20_000,
    "RIPPLE_DETECTION": 15_000,
    "QUESTION_ANSWER": 15_000,
}
DEFAULT_PROVIDER_STAGES: tuple[str, ...] = (
    "planning",
    "annotation",
    "execution",
    "debug",
    "question_answer",
    "ripple_detection",
    "alter_annotation",
    "knowledge",
)
DEFAULT_STAGE_MODELS: dict[str, dict[str, str]] = {
    "claude": {
        "default": "claude-sonnet-4-6",
        "planning": "claude-sonnet-4-6",
        "annotation": "claude-sonnet-4-6",
        "execution": "claude-sonnet-4-6",
        "debug": "claude-sonnet-4-6",
    },
    "openai": {
        "default": "o3-mini",
        "planning": "o3-mini",
        "annotation": "gpt-4o-mini",
        "execution": "gpt-4o",
        "debug": "gpt-4o-mini",
    },
    "groq": {
        "default": "llama-3.3-70b-versatile",
        "planning": "llama-3.3-70b-versatile",
        "annotation": "llama-3.1-8b-instant",
        "execution": "llama-3.3-70b-versatile",
        "debug": "llama-3.1-8b-instant",
    },
    "ollama": {
        "default": "llama3.2",
        "planning": "llama3.2",
        "annotation": "llama3.2",
        "execution": "llama3.2",
        "debug": "llama3.2",
    },
    "huggingface": {"default": ""},
    "mock": {"default": ""},
}


@dataclass(frozen=True)
class ProviderConnection:
    style: str
    base_url: str
    secret_ref: str
    api_key: str = ""


@dataclass(frozen=True)
class ProviderFeatures:
    tools: bool = True
    skills: bool = True
    checkpoints: bool = True
    subagents: bool = True
    summarization: bool = True


@dataclass(frozen=True)
class ProviderRouting:
    use_for_stages: tuple[str, ...] = DEFAULT_PROVIDER_STAGES
    personas: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderOptimizations:
    openai: dict[str, Any] = field(default_factory=dict)
    anthropic: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    type: str
    enabled: bool
    label: str
    connection: ProviderConnection
    models: dict[str, str]
    features: ProviderFeatures
    optimizations: ProviderOptimizations
    routing: ProviderRouting

    def model_for_stage(self, stage: str) -> str:
        stage_key = stage.strip().lower()
        return (
            self.models.get(stage_key)
            or self.models.get("default")
            or DEFAULT_STAGE_MODELS.get(self.type, {}).get(stage_key)
            or DEFAULT_STAGE_MODELS.get(self.type, {}).get("default", "")
        )

    def supports_stage(self, stage: str) -> bool:
        stage_key = stage.strip().lower()
        allowed = set(self.routing.use_for_stages)
        if not allowed:
            return True
        if stage_key in allowed:
            return True
        if stage_key == "live_debug" and "debug" in allowed:
            return True
        return False

    def supports_persona(self, persona: str) -> bool:
        if not self.routing.personas:
            return True
        return persona.strip().lower() in {item.lower() for item in self.routing.personas}

    def provider_kind(self) -> str:
        if self.type == "claude":
            return "anthropic"
        return self.type


@dataclass(frozen=True)
class SubagentProviderOverride:
    """Maps a named subagent to a preferred provider and isolated session key prefix."""
    subagent_id: str
    provider_id: str
    session_key_prefix: str = ""


@dataclass(frozen=True)
class PersonaProviderAssignment:
    persona_id: str
    provider_id: str
    model: str = ""
    stages: tuple[str, ...] = DEFAULT_PROVIDER_STAGES

    def supports_stage(self, stage: str) -> bool:
        stage_key = normalize_stage_name(stage)
        allowed = set(self.stages)
        if not allowed:
            return True
        if stage_key in allowed:
            return True
        return stage_key == "debug" and "live_debug" in allowed


@dataclass(frozen=True)
class ProviderPolicies:
    fallback_provider_order: tuple[str, ...]
    session_key_strategy: str
    flush_on_task_complete: bool
    flush_on_provider_switch: bool
    reload_mode: str
    summarization_thresholds: dict[str, int]
    persona_assignments: tuple[PersonaProviderAssignment, ...] = ()
    persona_prompt_overrides: dict[str, str] = field(default_factory=dict)
    subagent_overrides: tuple[SubagentProviderOverride, ...] = ()


@dataclass(frozen=True)
class ProviderProfileDocument:
    version: int
    active_provider_id: str
    catalog: tuple[ProviderProfile, ...]
    policies: ProviderPolicies

    @property
    def profile_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "activeProviderId": self.active_provider_id,
            "catalog": [provider_to_dict(item) for item in self.catalog],
            "policies": {
                "fallbackProviderOrder": list(self.policies.fallback_provider_order),
                "sessionKeyStrategy": self.policies.session_key_strategy,
                "flushOnTaskComplete": self.policies.flush_on_task_complete,
                "flushOnProviderSwitch": self.policies.flush_on_provider_switch,
                "reloadMode": self.policies.reload_mode,
                "summarizationThresholds": dict(self.policies.summarization_thresholds),
                "personaAssignments": [
                    {
                        "personaId": assignment.persona_id,
                        "providerId": assignment.provider_id,
                        "model": assignment.model,
                        "stages": list(assignment.stages),
                    }
                    for assignment in self.policies.persona_assignments
                ],
                "personaPromptOverrides": dict(self.policies.persona_prompt_overrides),
                "subagentOverrides": [
                    {
                        "subagentId": o.subagent_id,
                        "providerId": o.provider_id,
                        "sessionKeyPrefix": o.session_key_prefix,
                    }
                    for o in self.policies.subagent_overrides
                ],
            },
        }


def default_provider_profile_document(provider_type: str) -> ProviderProfileDocument:
    normalized_type = normalize_provider_type(provider_type) or "claude"
    provider_id = {
        "claude": "anthropic-default",
        "openai": "openai-default",
        "groq": "groq-default",
        "ollama": "ollama-default",
        "huggingface": "huggingface-default",
        "mock": "mock-default",
    }[normalized_type]
    return normalize_provider_profile({
        "activeProviderId": provider_id,
        "catalog": [
            {
                "id": provider_id,
                "type": normalized_type,
                "enabled": True,
                "label": default_provider_label(normalized_type),
                "connection": {
                    "style": "local" if normalized_type == "ollama" else "native",
                    "baseUrl": "http://localhost:11434" if normalized_type == "ollama" else "",
                    "secretRef": f"waterfree.provider.{provider_id}.key",
                    "apiKey": "",
                },
                "models": DEFAULT_STAGE_MODELS.get(normalized_type, {"default": ""}),
                "features": {
                    "tools": True,
                    "skills": True,
                    "checkpoints": True,
                    "subagents": True,
                    "summarization": True,
                },
                "optimizations": {
                    "openai": normalize_openai_optimizations({}) if normalized_type == "openai" else {},
                    "anthropic": normalize_anthropic_optimizations({}) if normalized_type == "claude" else {},
                },
                "routing": {
                    "useForStages": list(DEFAULT_PROVIDER_STAGES),
                    "personas": [],
                },
            }
        ],
        "policies": {
            "fallbackProviderOrder": [provider_id],
            "sessionKeyStrategy": "workspace_stage_persona_provider",
            "flushOnTaskComplete": True,
            "flushOnProviderSwitch": True,
            "reloadMode": "on_change",
            "summarizationThresholds": DEFAULT_SUMMARIZATION_THRESHOLDS,
        },
    })


def provider_to_dict(profile: ProviderProfile) -> dict[str, Any]:
    raw = asdict(profile)
    raw["connection"]["baseUrl"] = raw["connection"].pop("base_url")
    raw["connection"]["secretRef"] = raw["connection"].pop("secret_ref")
    raw["routing"]["useForStages"] = list(raw["routing"].pop("use_for_stages"))
    raw["routing"]["personas"] = list(raw["routing"]["personas"])
    return raw


def load_provider_profile(workspace_path: str, *, raw: Any | None = None) -> ProviderProfileDocument:
    if raw is None:
        profile_path = Path(workspace_path).joinpath(*DEFAULT_PROFILE_PATH)
        try:
            raw = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    return normalize_provider_profile(raw)


def normalize_provider_profile(raw: Any) -> ProviderProfileDocument:
    source = raw if isinstance(raw, dict) else {}
    catalog_raw = source.get("catalog", []) if isinstance(source.get("catalog", []), list) else []
    catalog = tuple(
        entry for index, item in enumerate(catalog_raw)
        if (entry := normalize_provider_entry(item, f"provider-{index + 1}")) is not None
    )
    active_provider_id = str(source.get("activeProviderId", "") or "").strip()
    if active_provider_id and not any(item.id == active_provider_id for item in catalog):
        active_provider_id = ""
    if not active_provider_id:
        active_provider_id = next((item.id for item in catalog if item.enabled), catalog[0].id if catalog else "")

    policies_raw = source.get("policies", {}) if isinstance(source.get("policies"), dict) else {}
    fallback = tuple(
        item for item in dict.fromkeys(
            str(entry).strip()
            for entry in policies_raw.get("fallbackProviderOrder", [])
            if str(entry).strip()
        )
        if any(provider.id == item for provider in catalog)
    )
    completed_fallback = fallback + tuple(
        provider.id for provider in catalog if provider.id not in set(fallback)
    )
    policies = ProviderPolicies(
        fallback_provider_order=completed_fallback,
        session_key_strategy=str(policies_raw.get("sessionKeyStrategy", "workspace_stage_persona_provider")).strip()
        or "workspace_stage_persona_provider",
        flush_on_task_complete=policies_raw.get("flushOnTaskComplete", True) is not False,
        flush_on_provider_switch=policies_raw.get("flushOnProviderSwitch", True) is not False,
        reload_mode="manual" if policies_raw.get("reloadMode") == "manual" else "on_change",
        summarization_thresholds=normalize_summarization_thresholds(
            policies_raw.get("summarizationThresholds", {})
        ),
        persona_assignments=_normalize_persona_assignments(
            policies_raw.get("personaAssignments"), catalog
        ),
        persona_prompt_overrides=_normalize_persona_prompt_overrides(
            policies_raw.get("personaPromptOverrides")
        ),
        subagent_overrides=_normalize_subagent_overrides(
            policies_raw.get("subagentOverrides", []), catalog
        ),
    )
    return ProviderProfileDocument(
        version=1,
        active_provider_id=active_provider_id,
        catalog=catalog,
        policies=policies,
    )


def normalize_provider_entry(raw: Any, fallback_id: str) -> ProviderProfile | None:
    if not isinstance(raw, dict):
        return None
    provider_type = normalize_provider_type(raw.get("type"))
    if not provider_type:
        return None
    provider_id = str(raw.get("id", "") or fallback_id).strip() or fallback_id
    connection_raw = raw.get("connection", {}) if isinstance(raw.get("connection"), dict) else {}
    base_url = normalize_base_url(provider_type, connection_raw.get("baseUrl", ""))
    connection = ProviderConnection(
        style=normalize_connection_style(provider_type, connection_raw.get("style"), base_url),
        base_url=base_url,
        secret_ref=str(connection_raw.get("secretRef", "") or f"waterfree.provider.{provider_id}.key").strip(),
        api_key=str(connection_raw.get("apiKey", "") or "").strip(),
    )
    models = normalize_stage_models(provider_type, raw.get("models"))
    routing_raw = raw.get("routing", {}) if isinstance(raw.get("routing"), dict) else {}
    stages = tuple(
        stage for stage in (
            normalize_stage_name(item) for item in routing_raw.get("useForStages", [])
        ) if stage
    ) or DEFAULT_PROVIDER_STAGES
    personas = tuple(
        str(item).strip().lower() for item in routing_raw.get("personas", []) if str(item).strip()
    )
    features_raw = raw.get("features", {}) if isinstance(raw.get("features"), dict) else {}
    optimizations_raw = raw.get("optimizations", {}) if isinstance(raw.get("optimizations"), dict) else {}
    return ProviderProfile(
        id=provider_id,
        type=provider_type,
        enabled=raw.get("enabled", True) is not False,
        label=str(raw.get("label", "") or default_provider_label(provider_type)).strip(),
        connection=connection,
        models=models,
        features=ProviderFeatures(
            tools=features_raw.get("tools", True) is not False,
            skills=features_raw.get("skills", True) is not False,
            checkpoints=features_raw.get("checkpoints", True) is not False,
            subagents=features_raw.get("subagents", True) is not False,
            summarization=features_raw.get("summarization", True) is not False,
        ),
        optimizations=ProviderOptimizations(
            openai=normalize_openai_optimizations(optimizations_raw.get("openai", {})) if provider_type == "openai" else {},
            anthropic=normalize_anthropic_optimizations(optimizations_raw.get("anthropic", {})) if provider_type == "claude" else {},
        ),
        routing=ProviderRouting(use_for_stages=stages, personas=personas),
    )


def normalize_provider_type(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in {"claude", "anthropic"}:
        return "claude"
    if value in {"openai", "codex", "chatgpt"}:
        return "openai"
    if value in {"groq"}:
        return "groq"
    if value == "ollama":
        return "ollama"
    if value in {"huggingface", "hf"}:
        return "huggingface"
    if value == "mock":
        return "mock"
    return ""


def normalize_connection_style(provider_type: str, raw: Any, base_url: str) -> str:
    value = str(raw or "").strip().lower()
    if provider_type == "mock":
        return "none"
    if provider_type == "ollama":
        return "local"
    if value in {"native", "compatible", "local", "none"}:
        return value
    if base_url:
        return "local" if "localhost" in base_url or "127.0.0.1" in base_url else "compatible"
    return "native"


def normalize_base_url(provider_type: str, raw: Any) -> str:
    url = str(raw or "").strip().rstrip("/")
    if provider_type == "ollama":
        return url or "http://localhost:11434"
    return url


def normalize_stage_models(provider_type: str, raw: Any) -> dict[str, str]:
    defaults = dict(DEFAULT_STAGE_MODELS.get(provider_type, {"default": ""}))
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(value or "").strip():
                defaults[str(key).strip().lower()] = str(value).strip()
    elif isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
        if values:
            defaults["default"] = values[0]
            defaults["planning"] = values[0]
            defaults["annotation"] = values[1] if len(values) > 1 else values[0]
            defaults["execution"] = values[0]
            defaults["debug"] = values[1] if len(values) > 1 else values[0]
    defaults.setdefault("default", "")
    return defaults


def normalize_stage_name(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    aliases = {
        "live_debug": "debug",
        "question": "question_answer",
    }
    return aliases.get(value, value)


def normalize_openai_optimizations(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    retention = source.get("promptCacheRetention")
    return {
        "useResponsesApi": source.get("useResponsesApi", True) is not False,
        "usePreviousResponseId": source.get("usePreviousResponseId", True) is not False,
        "promptCacheKeyStrategy": str(source.get("promptCacheKeyStrategy", "session_stage_persona")).strip()
        or "session_stage_persona",
        "promptCacheRetention": None if retention in {None, ""} else str(retention).strip(),
        "streamUsage": source.get("streamUsage", True) is not False,
    }


def normalize_anthropic_optimizations(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    budget = source.get("thinkingBudgetTokens", 10_000)
    try:
        budget_int = int(budget)
    except (TypeError, ValueError):
        budget_int = 10_000
    return {
        "enablePromptCaching": source.get("enablePromptCaching", True) is not False,
        # Extended thinking — off by default; enable in providers.json for planning stages.
        # NOTE: extended thinking and prompt caching are mutually exclusive per Anthropic docs.
        "extendedThinking": source.get("extendedThinking", False) is True,
        "thinkingBudgetTokens": max(1_024, budget_int),
    }


def normalize_summarization_thresholds(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return dict(DEFAULT_SUMMARIZATION_THRESHOLDS)
    result: dict[str, int] = {}
    for key, value in raw.items():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result[str(key).upper()] = number
    return result or dict(DEFAULT_SUMMARIZATION_THRESHOLDS)


def _normalize_subagent_overrides(
    raw: Any, catalog: tuple[ProviderProfile, ...]
) -> tuple[SubagentProviderOverride, ...]:
    if not isinstance(raw, list):
        return ()
    valid_ids = {p.id for p in catalog}
    overrides: list[SubagentProviderOverride] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        subagent_id = str(item.get("subagentId", "") or "").strip()
        provider_id = str(item.get("providerId", "") or "").strip()
        if not subagent_id or not provider_id:
            continue
        if provider_id not in valid_ids:
            continue
        if subagent_id in seen:
            continue
        seen.add(subagent_id)
        overrides.append(SubagentProviderOverride(
            subagent_id=subagent_id,
            provider_id=provider_id,
            session_key_prefix=str(item.get("sessionKeyPrefix", "") or "").strip(),
        ))
    return tuple(overrides)


def _normalize_persona_assignments(
    raw: Any, catalog: tuple[ProviderProfile, ...]
) -> tuple[PersonaProviderAssignment, ...]:
    valid_ids = {p.id for p in catalog}
    assignments: list[PersonaProviderAssignment] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            persona_id = str(item.get("personaId", "") or "").strip().lower()
            provider_id = str(item.get("providerId", "") or "").strip()
            if not persona_id or provider_id not in valid_ids:
                continue
            stages = tuple(dict.fromkeys(
                stage for stage in (
                    normalize_stage_name(entry) for entry in item.get("stages", [])
                ) if stage
            )) or DEFAULT_PROVIDER_STAGES
            assignments.append(PersonaProviderAssignment(
                persona_id=persona_id,
                provider_id=provider_id,
                model=str(item.get("model", "") or "").strip(),
                stages=stages,
            ))
        return tuple(assignments)

    # Legacy migration: provider.routing.personas used to carry persona affinity.
    for provider in catalog:
        if not provider.routing.personas:
            continue
        stages = provider.routing.use_for_stages or DEFAULT_PROVIDER_STAGES
        model = provider.models.get("default", "")
        for persona_id in provider.routing.personas:
            assignments.append(PersonaProviderAssignment(
                persona_id=persona_id.strip().lower(),
                provider_id=provider.id,
                model=model,
                stages=tuple(dict.fromkeys(stages)) or DEFAULT_PROVIDER_STAGES,
            ))
    return tuple(assignments)


def _normalize_persona_prompt_overrides(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in raw.items():
        persona_id = str(key or "").strip().lower()
        prompt = str(value or "").strip()
        if persona_id and prompt:
            result[persona_id] = prompt
    return result


def default_provider_label(provider_type: str) -> str:
    return {
        "claude": "Claude",
        "openai": "OpenAI",
        "groq": "Groq",
        "ollama": "Ollama",
        "huggingface": "Hugging Face",
        "mock": "Mock",
    }.get(provider_type, provider_type.title())
