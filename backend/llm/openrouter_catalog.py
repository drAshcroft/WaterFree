"""Live OpenRouter model discovery, with an on-disk cache.

OpenRouter's catalog is large and churns constantly: free variants appear and
disappear, and the cheapest paid model this week is not the cheapest next week.
Hardcoding ids therefore goes stale in a way the rest of `model_catalog.py`
(fixed vendor line-ups) does not, so the reader stages resolve their model at
run time from `GET /api/v1/models`.

Two pseudo-model sentinels drive this, usable anywhere a model id is accepted
in `.waterfree/providers.json`:

    auto:free    zero-priced models only, best context window first
    auto:floor   cheapest priced models first (the "price floor")

Both expand to an *ordered candidate list*, not a single id -- free endpoints
are heavily rate limited, so the caller walks the list on failure (see
`chat_client.chat`). `auto:free` deliberately appends a short floor tail,
because a free tier that is 429-ing all day is not a working configuration.

Stdlib only: this is imported by the standalone `waterfree` CLI. See the module
docstring in `chat_client.py` for why that constraint exists.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from backend.llm.provider_profiles import (
    DEFAULT_PROFILE_PATH,
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_BASE_URL,
)

log = logging.getLogger(__name__)

# Sentinels accepted in place of a concrete model id.
AUTO_FREE = "auto:free"
AUTO_FLOOR = "auto:floor"
AUTO_SENTINELS: frozenset[str] = frozenset({AUTO_FREE, AUTO_FLOOR})

CACHE_FILENAME = "openrouter-models.json"
# A day is well inside the rate at which OpenRouter's catalog turns over, and
# keeps a cold `waterfree qa-summary` from paying for a catalog fetch per run.
DEFAULT_TTL_SECONDS = int(os.environ.get("WATERFREE_OPENROUTER_CATALOG_TTL", str(24 * 3600)))
_FETCH_TIMEOUT_SECONDS = 20
# Chunked readers need room for one chunk plus the prompt scaffolding. Anything
# below this is not a usable reader model regardless of price.
MIN_READER_CONTEXT = 16_000
# How many candidates a sentinel expands to. Enough to ride out a couple of
# rate-limited free endpoints without turning one failed run into 20 requests.
DEFAULT_CANDIDATE_LIMIT = 4
# Paid tail appended to an auto:free expansion.
_FREE_PAID_TAIL = 2


class CatalogUnavailable(RuntimeError):
    """The catalog could not be fetched and no usable cache exists."""


@dataclass(frozen=True)
class OpenRouterModel:
    id: str
    name: str
    context_length: int
    prompt_cost: float          # USD per token (OpenRouter's native unit)
    completion_cost: float
    supports_tools: bool
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)

    @property
    def is_free(self) -> bool:
        return self.prompt_cost <= 0.0 and self.completion_cost <= 0.0

    @property
    def is_text_chat(self) -> bool:
        """Text in, text *only* out.

        The catalog is not just chat models. Media models are the trap here:
        `google/lyria-3-pro-preview` bills $0.08 per generated song but reports
        per-token pricing of "0" and a 1M context window, so a naive
        "free, widest context" pick lands on a music generator. Requiring
        text-only output excludes those without needing a vendor blocklist.
        """
        return "text" in self.input_modalities and tuple(self.output_modalities) == ("text",)

    @property
    def blended_cost(self) -> float:
        """Cost proxy for ranking: readers are input-heavy, roughly 4:1 in:out."""
        return self.prompt_cost * 4.0 + self.completion_cost


def is_auto_model(model_id: str) -> bool:
    return str(model_id or "").strip().lower() in AUTO_SENTINELS


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def select_candidates(
    models: list[OpenRouterModel],
    *,
    sentinel: str,
    min_context: int = MIN_READER_CONTEXT,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    require_tools: bool = False,
) -> list[str]:
    """Expand a sentinel into an ordered list of concrete model ids."""
    normalized = str(sentinel or "").strip().lower()
    if normalized not in AUTO_SENTINELS:
        raise ValueError(f"Not an auto-model sentinel: {sentinel!r}")

    usable = [
        m for m in models
        if m.is_text_chat
        and m.context_length >= min_context
        and (m.supports_tools or not require_tools)
    ]
    free = sorted(
        (m for m in usable if m.is_free),
        # Widest context first: fewer chunks per document is both faster and
        # cheaper in requests, which is the binding constraint on free tiers.
        key=lambda m: (-m.context_length, m.id),
    )
    paid = sorted(
        (m for m in usable if not m.is_free),
        key=lambda m: (m.blended_cost, -m.context_length, m.id),
    )

    if normalized == AUTO_FREE:
        # Free first; a short paid tail so a rate-limited free tier degrades to
        # a cent-scale model rather than to a hard failure.
        ordered = free[:limit] + paid[:_FREE_PAID_TAIL]
    else:
        ordered = paid[:limit]

    seen: set[str] = set()
    result: list[str] = []
    for model in ordered:
        if model.id not in seen:
            seen.add(model.id)
            result.append(model.id)
    return result


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

def load_models(
    *,
    workspace_path: str = "",
    api_key: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    force_refresh: bool = False,
) -> list[OpenRouterModel]:
    """Return the catalog, preferring a fresh cache over a network call.

    A stale cache beats a failed fetch: if the network call raises, an expired
    cache is still served (with a warning) so an offline run keeps working
    against ids that were valid yesterday.
    """
    cache_path = _cache_path(workspace_path)
    cached, age = _read_cache(cache_path)
    if cached and not force_refresh and age is not None and age < ttl_seconds:
        return cached

    try:
        models = _fetch_models(api_key=api_key)
    except CatalogUnavailable as exc:
        if cached:
            log.warning(
                "OpenRouter catalog refresh failed (%s); using cache aged %ss.",
                exc, int(age or 0),
            )
            return cached
        raise
    _write_cache(cache_path, models)
    return models


def _fetch_models(*, api_key: str = "") -> list[OpenRouterModel]:
    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/models"
    headers = {"Accept": "application/json"}
    # /models is public; the key is sent only when present so that per-account
    # visibility is respected.
    key = str(api_key or "").strip() or os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise CatalogUnavailable(f"OpenRouter /models returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CatalogUnavailable(f"Cannot reach OpenRouter /models: {exc}") from exc
    except Exception as exc:
        raise CatalogUnavailable(f"OpenRouter /models request failed: {exc}") from exc

    models = parse_models(payload)
    if not models:
        raise CatalogUnavailable("OpenRouter /models returned no usable entries.")
    return models


def parse_models(payload: object) -> list[OpenRouterModel]:
    """Normalize the `/models` body. Unparseable entries are skipped, not fatal."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    models: list[OpenRouterModel] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id", "") or "").strip()
        if not model_id:
            continue
        raw_pricing = entry.get("pricing")
        pricing = raw_pricing if isinstance(raw_pricing, dict) else {}
        supported = entry.get("supported_parameters")
        raw_arch = entry.get("architecture")
        architecture = raw_arch if isinstance(raw_arch, dict) else {}
        models.append(OpenRouterModel(
            id=model_id,
            name=str(entry.get("name", "") or model_id),
            context_length=_as_int(entry.get("context_length")),
            prompt_cost=_as_price(pricing.get("prompt")),
            completion_cost=_as_price(pricing.get("completion")),
            supports_tools=bool(isinstance(supported, list) and "tools" in supported),
            input_modalities=_modalities(architecture.get("input_modalities")),
            output_modalities=_modalities(architecture.get("output_modalities")),
        ))
    return models


def _modalities(value: object) -> tuple[str, ...]:
    """Default to text when the field is absent -- an entry with no declared
    modalities is a plain chat model, not an unusable one."""
    if not isinstance(value, list):
        return ("text",)
    items = tuple(str(v).strip().lower() for v in value if str(v).strip())
    return items or ("text",)


def _as_int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _as_price(value: object) -> float:
    """OpenRouter prices are decimal strings; a negative value means "variable"."""
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return float("inf")
    # Treat unknown pricing as expensive rather than free -- a variable-priced
    # model must never be mistaken for a free one.
    return float("inf") if parsed < 0 else parsed


def _cache_path(workspace_path: str) -> Path:
    root = Path(workspace_path).resolve() if workspace_path else Path.cwd()
    return root.joinpath(*DEFAULT_PROFILE_PATH[:-1], CACHE_FILENAME)


def _read_cache(path: Path) -> tuple[list[OpenRouterModel], float | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    if not isinstance(raw, dict):
        return [], None
    models = parse_models({"data": raw.get("data")})
    if not models:
        return [], None
    try:
        fetched_at = float(raw.get("fetchedAt") or 0.0)
    except (TypeError, ValueError):
        fetched_at = 0.0
    age = max(0.0, time.time() - fetched_at) if fetched_at else None
    return models, age


def _write_cache(path: Path, models: list[OpenRouterModel]) -> None:
    body = {
        "fetchedAt": time.time(),
        "data": [
            {
                "id": m.id,
                "name": m.name,
                "context_length": m.context_length,
                "pricing": {"prompt": str(m.prompt_cost), "completion": str(m.completion_cost)},
                "supported_parameters": ["tools"] if m.supports_tools else [],
                "architecture": {
                    "input_modalities": list(m.input_modalities),
                    "output_modalities": list(m.output_modalities),
                },
            }
            for m in models
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")
    except OSError as exc:
        # A read-only workspace should cost a cache, not the run.
        log.debug("Could not write OpenRouter catalog cache to %s: %s", path, exc)
