"""Provider selection helpers for runtime stages and personas."""

from __future__ import annotations

from dataclasses import dataclass

from backend.llm.provider_profiles import ProviderProfile, ProviderProfileDocument


@dataclass(frozen=True)
class ResolvedProvider:
    profile: ProviderProfile
    runtime_name: str


def resolve_provider(
    document: ProviderProfileDocument,
    *,
    stage: str,
    persona: str,
    preferred_runtime: str = "",
    provider_id: str = "",
) -> ResolvedProvider | None:
    candidates = [
        item for item in ordered_providers(document)
        if item.enabled
        and item.supports_stage(stage)
        and item.supports_persona(persona)
        and runtime_matches(item, preferred_runtime)
    ]
    if provider_id:
        explicit = next((item for item in candidates if item.id == provider_id), None)
        if explicit is not None:
            return ResolvedProvider(profile=explicit, runtime_name=runtime_name_for_provider(explicit))
    active = next((item for item in candidates if item.id == document.active_provider_id), None)
    if active is not None:
        return ResolvedProvider(profile=active, runtime_name=runtime_name_for_provider(active))
    if candidates:
        chosen = candidates[0]
        return ResolvedProvider(profile=chosen, runtime_name=runtime_name_for_provider(chosen))
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
