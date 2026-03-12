"""
Usage accumulator and persistent store for token stats.

UsageStore persists cumulative token counts per provider to
.waterfree/usage.json so that future sessions can read them.
The schema is intentionally simple so the frontend can render it
without knowing anything about which providers are configured.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from backend.llm.channels.base import UsageStats

log = logging.getLogger(__name__)


@dataclass
class ProviderUsage:
    provider: str
    provider_type: str = ""
    model: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    call_count: int = 0

    def add(self, stats: UsageStats, *, provider_type: str = "", model: str = "") -> None:
        if provider_type:
            self.provider_type = provider_type
        if model:
            self.model = model
        self.total_input_tokens += stats.input_tokens
        self.total_output_tokens += stats.output_tokens
        self.total_cache_read_tokens += stats.cache_read_tokens
        self.total_cache_creation_tokens += stats.cache_creation_tokens
        self.call_count += 1

    @property
    def total_processed_input_tokens(self) -> int:
        """Total input tokens across all pricing tiers (uncached + creation + read)."""
        return self.total_input_tokens + self.total_cache_creation_tokens + self.total_cache_read_tokens

    @property
    def cache_hit_rate(self) -> float:
        total = self.total_processed_input_tokens
        return self.total_cache_read_tokens / total if total else 0.0

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "providerType": self.provider_type,
            "model": self.model,
            # Tokens billed at full price (not served from cache)
            "totalInputTokens": self.total_input_tokens,
            "totalOutputTokens": self.total_output_tokens,
            # Tokens written to cache (billed at 1.25× input price for Anthropic)
            "totalCacheCreationTokens": self.total_cache_creation_tokens,
            # Tokens served from cache (billed at 0.10× input price for Anthropic)
            "totalCacheReadTokens": self.total_cache_read_tokens,
            # Derived: full volume of input processed across all tiers
            "totalProcessedInputTokens": self.total_processed_input_tokens,
            "cacheHitRate": round(self.cache_hit_rate, 4),
            "callCount": self.call_count,
        }


class UsageStore:
    """
    Persists cumulative usage to .waterfree/usage.json.

    One instance is shared across all channels for a given workspace.
    Thread-safety is not required; the backend runs single-threaded.
    """

    def __init__(self, workspace_path: str) -> None:
        self._path = Path(workspace_path) / ".waterfree" / "usage.json"
        self._by_provider: dict[str, ProviderUsage] = self._load()

    def record(self, provider: str, stats: UsageStats, *, provider_type: str = "", model: str = "") -> None:
        """Add stats to the named provider's running totals and flush to disk."""
        if provider not in self._by_provider:
            self._by_provider[provider] = ProviderUsage(provider=provider)
        self._by_provider[provider].add(stats, provider_type=provider_type, model=model)
        self._flush()

    def get_all(self) -> list[dict]:
        return [pu.to_dict() for pu in self._by_provider.values()]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, ProviderUsage]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        result: dict[str, ProviderUsage] = {}
        for entry in raw.get("providers", []):
            p = entry.get("provider", "")
            if not p:
                continue
            result[p] = ProviderUsage(
                provider=p,
                provider_type=str(entry.get("providerType", "") or ""),
                model=str(entry.get("model", "") or ""),
                total_input_tokens=int(entry.get("totalInputTokens", 0)),
                total_output_tokens=int(entry.get("totalOutputTokens", 0)),
                total_cache_read_tokens=int(entry.get("totalCacheReadTokens", 0)),
                total_cache_creation_tokens=int(entry.get("totalCacheCreationTokens", 0)),
                call_count=int(entry.get("callCount", 0)),
            )
        return result

    def _flush(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"providers": self.get_all()}, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Could not persist usage stats: %s", exc)
