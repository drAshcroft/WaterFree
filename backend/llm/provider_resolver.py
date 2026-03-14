"""Provider selection helpers for runtime stages and personas."""

from __future__ import annotations

from dataclasses import dataclass

from backend.llm.personas import get_persona
from backend.llm.provider_profiles import ProviderProfile, ProviderProfileDocument, SubagentProviderOverride


@dataclass(frozen=True)
class ResolvedProvider:
    profile: ProviderProfile
    runtime_name: str
    model_name: str


def resolve_provider(
    document: ProviderProfileDocument,
    *,
    stage: str,
    persona: str,
    preferred_runtime: str = "",
    provider_id: str = "",
    subagent_id: str = "",
) -> ResolvedProvider | None:
    ordered = ordered_providers(document)
    eligible = [
        item for item in ordered
        if item.enabled and runtime_matches(item, preferred_runtime)
    ]

    # Subagent overrides take priority over all other resolution, keeping
    # subagents' token accounting isolated under their own provider/session.
    if subagent_id:
        override = _find_subagent_override(document, subagent_id)
        if override is not None:
            overridden = next((c for c in eligible if c.id == override.provider_id), None)
            if overridden is not None:
                return _resolved(overridden, stage=stage)

    if provider_id:
        explicit = next((item for item in eligible if item.id == provider_id), None)
        if explicit is not None:
            return _resolved(explicit, stage=stage)

    tier_routed = _find_persona_tier_route(document, eligible, stage=stage, persona=persona)
    if tier_routed is not None:
        return tier_routed

    candidates = [
        item for item in eligible
        if item.supports_stage(stage) and item.supports_persona(persona)
    ]
    if candidates:
        chosen = candidates[0]
        return _resolved(chosen, stage=stage)
    return None


def _find_subagent_override(
    document: ProviderProfileDocument, subagent_id: str
) -> SubagentProviderOverride | None:
    for override in document.policies.subagent_overrides:
        if override.subagent_id == subagent_id:
            return override
    return None


def ordered_providers(document: ProviderProfileDocument) -> list[ProviderProfile]:
    by_id = {item.id: item for item in document.catalog}
    ordered: list[ProviderProfile] = []
    for provider_id in document.policies.fallback_provider_order:
        provider = by_id.get(provider_id)
        if provider is not None and provider not in ordered:
            ordered.append(provider)
    for provider in document.catalog:
        if provider not in ordered:
            ordered.append(provider)
    return ordered


def runtime_name_for_provider(profile: ProviderProfile) -> str:
    if profile.type == "openai":
        return "openai"
    if profile.type == "ollama":
        return "ollama"
    if profile.type == "huggingface":
        return "huggingface"
    if profile.type == "mock":
        return "mock"
    return "deep_agents"


def runtime_matches(profile: ProviderProfile, preferred_runtime: str) -> bool:
    if not preferred_runtime:
        return True
    if preferred_runtime == "deep_agents":
        return runtime_name_for_provider(profile) in {"deep_agents", "openai", "ollama"}
    return runtime_name_for_provider(profile) == preferred_runtime


def _find_persona_assignment(
    document: ProviderProfileDocument,
    eligible: list[ProviderProfile],
    *,
    stage: str,
    persona: str,
) -> ResolvedProvider | None:
    persona_key = persona.strip().lower()
    by_id = {item.id: item for item in eligible}
    for assignment in document.policies.persona_assignments:
        if assignment.persona_id != persona_key:
            continue
        if not assignment.supports_stage(stage):
            continue
        profile = by_id.get(assignment.provider_id)
        if profile is None:
            continue
        return _resolved(profile, stage=stage, model_name=assignment.model)
    return None


def _find_persona_tier_route(
    document: ProviderProfileDocument,
    eligible: list[ProviderProfile],
    *,
    stage: str,
    persona: str,
) -> ResolvedProvider | None:
    persona_def = get_persona(persona)
    if persona_def is None:
        return None
    stage_key = stage.strip().upper()
    preferred_tiers = persona_def.preferred_model_tiers.get(stage_key, [])
    if not preferred_tiers:
        return None
    by_id = {item.id: item for item in eligible}
    for tier in preferred_tiers:
        route = document.policies.model_tier_routes.get(tier.strip().lower())
        if route is None:
            continue
        profile = by_id.get(route.provider_id)
        if profile is None:
            continue
        return _resolved(profile, stage=stage, model_name=route.model)
    return None


def _resolved(profile: ProviderProfile, *, stage: str, model_name: str = "") -> ResolvedProvider:
    # Priority 1: explicit model pin (from persona assignment or caller)
    if model_name.strip():
        return ResolvedProvider(
            profile=profile,
            runtime_name=runtime_name_for_provider(profile),
            model_name=model_name.strip(),
        )

    stage_key = stage.strip().lower()

    # Priority 2: per-stage model override on the provider profile
    override = profile.model_overrides.get(stage_key, "")
    if override:
        return ResolvedProvider(
            profile=profile,
            runtime_name=runtime_name_for_provider(profile),
            model_name=override,
        )

    # Priority 3: tier-based catalog lookup via stage_tiers mapping
    stage_tier = profile.stage_tiers.get(stage_key, "")
    if stage_tier:
        # Deferred import avoids circular dependency (catalog → profiles → resolver)
        from backend.llm.model_catalog import select_model_with_fallback  # noqa: PLC0415
        descriptor = select_model_with_fallback(
            [stage_tier],
            provider=profile.type,
        )
        if descriptor is not None:
            return ResolvedProvider(
                profile=profile,
                runtime_name=runtime_name_for_provider(profile),
                model_name=descriptor.id,
            )

    # Priority 4: legacy models dict fallback (unchanged behaviour)
    selected_model = profile.model_for_stage(stage)
    return ResolvedProvider(
        profile=profile,
        runtime_name=runtime_name_for_provider(profile),
        model_name=selected_model,
    )
