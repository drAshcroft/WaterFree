"""
Static model catalog for all supported providers.

Models are the primary query target; providers tag along to serve them.
Each ModelDescriptor carries intrinsic properties (tier, capabilities, cost,
rate limits, optimizations) so callers can find the right model without
knowing which provider is active.

TIER VOCABULARY
    canonical   aliases
    ─────────   ──────────────────
    apex        smartest
    balanced    (none)
    efficient   cheap, fast
    micro       small

Existing persona JSON files that use "smartest" / "cheap" are normalised at
runtime via TIER_ALIASES — no JSON edits needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Tier aliases — maps legacy / shorthand names to canonical tier names
# ---------------------------------------------------------------------------

TIER_ALIASES: dict[str, str] = {
    "smartest": "apex",
    "cheap": "efficient",
    "fast": "efficient",
    "small": "micro",
}

# Canonical tiers ordered highest → lowest quality
TIER_ORDER: list[str] = ["apex", "balanced", "efficient", "micro"]


def normalize_tier(tier: str) -> str:
    """Return canonical tier name, applying aliases."""
    t = tier.strip().lower()
    return TIER_ALIASES.get(t, t)


# ---------------------------------------------------------------------------
# ModelDescriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelDescriptor:
    """Intrinsic description of a single model.

    provider        — which provider type can serve this model
    tier            — canonical tier: apex | balanced | efficient | micro
    capabilities    — tuple of capability strings (see ModelCapability below)
    context_window  — total input context in tokens
    max_output      — max output tokens per request
    input_cost_per_1m  — USD per 1 M input tokens (0.0 for local models)
    output_cost_per_1m — USD per 1 M output tokens
    aliases         — alternate model id strings that map to this descriptor
    requests_per_minute / tokens_per_minute / tokens_per_day — rate limits
                      (0 = unknown / unlimited)
    optimizations   — provider-specific optimisation flags available for this
                      model (e.g. "extendedThinking", "promptCaching")
    """
    id: str
    provider: str                       # ProviderType literal
    tier: str                           # ModelTier literal
    capabilities: tuple[str, ...]
    context_window: int
    max_output: int
    input_cost_per_1m: float
    output_cost_per_1m: float
    aliases: tuple[str, ...] = ()
    requests_per_minute: int = 0
    tokens_per_minute: int = 0
    tokens_per_day: int = 0
    optimizations: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# MODEL_CATALOG
# Capability strings used:
#   "tools"        — function / tool calling
#   "vision"       — image / screenshot input (visual search)
#   "reasoning"    — extended thinking / chain-of-thought
#   "caching"      — prompt caching support
#   "streaming"    — streaming output
#   "json_mode"    — structured JSON output
#   "long_context" — >100 k token context window
# ---------------------------------------------------------------------------

MODEL_CATALOG: list[ModelDescriptor] = [

    # ── Anthropic / Claude ─────────────────────────────────────────────────

    ModelDescriptor(
        id="claude-opus-4-6",
        provider="claude",
        tier="apex",
        capabilities=("tools", "vision", "reasoning", "caching", "streaming", "json_mode", "long_context"),
        context_window=200_000,
        max_output=32_000,
        input_cost_per_1m=15.00,
        output_cost_per_1m=75.00,
        optimizations=("extendedThinking", "promptCaching"),
    ),
    ModelDescriptor(
        id="claude-sonnet-4-6",
        aliases=("claude-sonnet-4-5",),
        provider="claude",
        tier="balanced",
        capabilities=("tools", "vision", "caching", "streaming", "json_mode", "long_context"),
        context_window=200_000,
        max_output=16_000,
        input_cost_per_1m=3.00,
        output_cost_per_1m=15.00,
        optimizations=("promptCaching",),
    ),
    ModelDescriptor(
        id="claude-haiku-3-5",
        aliases=("claude-haiku-3-5-20251001",),
        provider="claude",
        tier="efficient",
        capabilities=("tools", "vision", "caching", "streaming", "json_mode", "long_context"),
        context_window=200_000,
        max_output=8_192,
        input_cost_per_1m=0.80,
        output_cost_per_1m=4.00,
        optimizations=("promptCaching",),
    ),

    # ── OpenAI ─────────────────────────────────────────────────────────────

    ModelDescriptor(
        id="o3",
        provider="openai",
        tier="apex",
        capabilities=("tools", "reasoning", "streaming", "json_mode", "long_context"),
        context_window=200_000,
        max_output=100_000,
        input_cost_per_1m=10.00,
        output_cost_per_1m=40.00,
        optimizations=("responsesAPI",),
    ),
    ModelDescriptor(
        id="o1",
        provider="openai",
        tier="apex",
        capabilities=("tools", "reasoning", "streaming", "json_mode", "long_context"),
        context_window=200_000,
        max_output=100_000,
        input_cost_per_1m=15.00,
        output_cost_per_1m=60.00,
        optimizations=("responsesAPI",),
    ),
    ModelDescriptor(
        id="gpt-4o",
        provider="openai",
        tier="balanced",
        capabilities=("tools", "vision", "caching", "streaming", "json_mode", "long_context"),
        context_window=128_000,
        max_output=16_384,
        input_cost_per_1m=2.50,
        output_cost_per_1m=10.00,
        optimizations=("promptCaching", "responsesAPI"),
    ),
    ModelDescriptor(
        id="o3-mini",
        provider="openai",
        tier="balanced",
        capabilities=("tools", "reasoning", "streaming", "json_mode", "long_context"),
        context_window=200_000,
        max_output=100_000,
        input_cost_per_1m=1.10,
        output_cost_per_1m=4.40,
        optimizations=("responsesAPI",),
    ),
    ModelDescriptor(
        id="gpt-4o-mini",
        provider="openai",
        tier="efficient",
        capabilities=("tools", "vision", "caching", "streaming", "json_mode", "long_context"),
        context_window=128_000,
        max_output=16_384,
        input_cost_per_1m=0.15,
        output_cost_per_1m=0.60,
        optimizations=("promptCaching", "responsesAPI"),
    ),

    # ── Groq ───────────────────────────────────────────────────────────────

    ModelDescriptor(
        id="llama-3.3-70b-versatile",
        provider="groq",
        tier="balanced",
        capabilities=("tools", "streaming", "json_mode"),
        context_window=128_000,
        max_output=32_768,
        input_cost_per_1m=0.59,
        output_cost_per_1m=0.79,
        tokens_per_minute=6_000,
        tokens_per_day=100_000,
    ),
    ModelDescriptor(
        id="llama-3.1-8b-instant",
        provider="groq",
        tier="efficient",
        capabilities=("tools", "streaming", "json_mode"),
        context_window=128_000,
        max_output=8_000,
        input_cost_per_1m=0.05,
        output_cost_per_1m=0.08,
        tokens_per_minute=20_000,
        tokens_per_day=500_000,
    ),

    # ── Ollama (local — cost is always 0) ─────────────────────────────────

    ModelDescriptor(
        id="llama3.2",
        provider="ollama",
        tier="efficient",
        capabilities=("tools", "streaming"),
        context_window=128_000,
        max_output=8_192,
        input_cost_per_1m=0.0,
        output_cost_per_1m=0.0,
    ),
    ModelDescriptor(
        id="llama3.1",
        provider="ollama",
        tier="balanced",
        capabilities=("tools", "streaming"),
        context_window=128_000,
        max_output=8_192,
        input_cost_per_1m=0.0,
        output_cost_per_1m=0.0,
    ),
    ModelDescriptor(
        id="phi3",
        aliases=("phi-3-mini",),
        provider="ollama",
        tier="micro",
        capabilities=("streaming",),
        context_window=4_096,
        max_output=4_096,
        input_cost_per_1m=0.0,
        output_cost_per_1m=0.0,
    ),
]

# ---------------------------------------------------------------------------
# Lookup indexes (built once at import time)
# ---------------------------------------------------------------------------

_BY_ID: dict[str, ModelDescriptor] = {}
for _m in MODEL_CATALOG:
    _BY_ID[_m.id] = _m
    for _alias in _m.aliases:
        _BY_ID[_alias] = _m


def get_model(model_id: str) -> ModelDescriptor | None:
    """Return descriptor for a model id (or alias), or None if unknown."""
    return _BY_ID.get(model_id.strip())


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def select_model(
    *,
    tier: str | list[str] | None = None,
    capabilities: list[str] | None = None,
    provider: str | None = None,
    min_context_window: int = 0,
    max_cost_per_1m: float = 0.0,
    prefer_lowest_cost: bool = False,
    prefer_highest_context: bool = False,
) -> ModelDescriptor | None:
    """Return the best model matching the query, or None.

    tier            — canonical or alias tier name(s)
    capabilities    — all listed capabilities must be present
    provider        — restrict to this provider type
    min_context_window — minimum context size in tokens
    max_cost_per_1m — maximum input cost (0 = no limit)
    prefer_lowest_cost / prefer_highest_context — tie-breaking
    """
    tiers_normalized: list[str] = []
    if tier is not None:
        raw_tiers = [tier] if isinstance(tier, str) else list(tier)
        tiers_normalized = [normalize_tier(t) for t in raw_tiers]

    candidates = [
        m for m in MODEL_CATALOG
        if (not tiers_normalized or m.tier in tiers_normalized)
        and (not provider or m.provider == provider)
        and (not capabilities or all(c in m.capabilities for c in capabilities))
        and (not min_context_window or m.context_window >= min_context_window)
        and (not max_cost_per_1m or m.input_cost_per_1m <= max_cost_per_1m)
    ]
    if not candidates:
        return None
    if prefer_lowest_cost:
        candidates.sort(key=lambda m: (m.input_cost_per_1m, m.output_cost_per_1m))
    elif prefer_highest_context:
        candidates.sort(key=lambda m: -m.context_window)
    return candidates[0]


def select_model_with_fallback(
    tiers: list[str],
    *,
    provider: str | None = None,
    capabilities: list[str] | None = None,
) -> ModelDescriptor | None:
    """Try each tier in order; degrade through TIER_ORDER if nothing matches.

    tiers — preferred tiers (canonical or alias) in priority order
    """
    normalized = [normalize_tier(t) for t in tiers]
    for t in normalized:
        result = select_model(tier=t, provider=provider, capabilities=capabilities)
        if result:
            return result

    # Tier degradation: start one step below the last requested tier
    start_idx = 0
    if normalized:
        last = normalized[-1]
        if last in TIER_ORDER:
            start_idx = TIER_ORDER.index(last) + 1
    for fallback_tier in TIER_ORDER[start_idx:]:
        result = select_model(tier=fallback_tier, provider=provider, capabilities=capabilities)
        if result:
            return result
    return None
