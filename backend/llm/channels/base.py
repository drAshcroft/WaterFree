"""
Provider channel abstractions.

A ProviderChannel wraps one LLM provider and manages the full lifecycle of an
agent session: agent creation, context accumulation, caching, and usage tracking.

All channels return a ChannelResult containing the response text and a UsageStats
snapshot for that call.  Callers can also query cumulative usage via
get_cumulative_usage().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class UsageStats:
    """Token usage for a single channel invocation."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        """Total input tokens processed (uncached + cache-creation + cache-read)."""
        return self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of total input served from cache (0.0–1.0)."""
        total = self.total_input_tokens
        return self.cache_read_tokens / total if total else 0.0

    def to_dict(self) -> dict:
        return {
            # Anthropic: tokens billed at full price (not from cache)
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            # Cache-creation tokens billed at 1.25× input price
            "cacheCreationTokens": self.cache_creation_tokens,
            # Cache-read tokens billed at 0.10× input price
            "cacheReadTokens": self.cache_read_tokens,
            # Derived: total input processed across all pricing tiers
            "totalInputTokens": self.total_input_tokens,
            "cacheHitRate": round(self.cache_hit_rate, 4),
        }

    def __add__(self, other: "UsageStats") -> "UsageStats":
        return UsageStats(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )


@dataclass
class ChannelResult:
    """Result of a single channel invocation."""

    text: str
    usage: UsageStats = field(default_factory=UsageStats)


@runtime_checkable
class ProviderChannel(Protocol):
    """Abstract interface every provider channel must satisfy."""

    def run(
        self,
        *,
        stage: str,
        prompt: str,
        persona: str,
        workspace_path: str,
        session_key: str = "",
    ) -> ChannelResult:
        """Execute one agent turn and return response text + usage."""
        ...

    def flush(self, session_key: str) -> None:
        """Discard cached agent state for this session (call when task is done)."""
        ...

    def get_cumulative_usage(self) -> dict:
        """Return cumulative usage for this channel since instantiation."""
        ...
