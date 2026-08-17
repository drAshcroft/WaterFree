"""
Tolerant enum parsing for stored payloads.

Task payloads in `.waterfree/tasks.db` are not written exclusively by this
codebase. Agents and external tooling have been caught writing rows straight
into the database using whatever todo vocabulary they happened to know — Claude
Code's `TodoWrite` says "completed" where `TaskStatus` says "complete" — and a
payload can also simply predate a rename on our side.

That matters more than it looks, because `Task.from_dict` runs inside
`TaskStore.__init__`. A strict `TaskStatus(...)` cast there turns a single bad
row into a workspace nobody can open *at all*: the board, the CLI,
`todos validate` and any repair tooling all die on the same line, and there is
no route back in through the product. So reads coerce instead of raising, and
record what they changed so `todos validate` can report it and the next write
can quietly normalize it.

Writes keep their teeth. `parse_enum` accepts the same aliases — normalization
has exactly one definition — but still rejects a value it has never heard of,
because a typo should fail at the source rather than land in the database as a
silent default.

Register aliases next to the enum they belong to, not here; this module has to
stay free of domain imports so anything can depend on it.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, TypeVar

E = TypeVar("E", bound=Enum)

# enum class -> {casefolded spelling: canonical member}. Populated at import
# time by whichever module defines the enum.
_ALIASES: dict[type[Enum], dict[str, Enum]] = {}


def register_enum_aliases(enum_cls: type[E], aliases: dict[str, E]) -> None:
    """Teach the coercion helpers a set of non-canonical spellings for `enum_cls`.

    Keys are matched casefolded and stripped, so only list genuine synonyms —
    case and surrounding whitespace are already handled.
    """
    table = _ALIASES.setdefault(enum_cls, {})
    for spelling, member in aliases.items():
        table[spelling.strip().casefold()] = member


def enum_aliases(enum_cls: type[E]) -> dict[str, E]:
    """The registered aliases for `enum_cls` — a copy, for callers that report them."""
    return dict(_ALIASES.get(enum_cls, {}))  # type: ignore[arg-type]


def _resolve(enum_cls: type[E], value: object) -> Optional[E]:
    """Canonical member for `value`, or None if nothing matches."""
    if isinstance(value, enum_cls):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    try:
        return enum_cls(text)
    except ValueError:
        pass

    folded = text.casefold()
    for member in enum_cls:
        if str(member.value).casefold() == folded:
            return member
    return _ALIASES.get(enum_cls, {}).get(folded)  # type: ignore[return-value]


def _valid_values(enum_cls: type[Enum]) -> str:
    return ", ".join(str(member.value) for member in enum_cls)


def coerce_enum(
    enum_cls: type[E],
    value: object,
    default: E,
    *,
    field: str,
    warnings: Optional[list[str]] = None,
) -> E:
    """Read path: never raises.

    Returns the canonical member for `value`, falling back to `default` when the
    value is missing or unrecognized. Anything that was not already canonical is
    appended to `warnings` so the caller can surface it.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default

    resolved = _resolve(enum_cls, value)
    if resolved is None:
        if warnings is not None:
            warnings.append(
                f"{field}: unknown value {value!r} — read as {default.value!r} "
                f"(expected one of: {_valid_values(enum_cls)})"
            )
        return default

    if not isinstance(value, enum_cls) and str(value) != str(resolved.value):
        if warnings is not None:
            warnings.append(f"{field}: {value!r} normalized to {resolved.value!r}")
    return resolved


def parse_enum(enum_cls: type[E], value: object, *, field: str) -> E:
    """Write path: accepts aliases, raises `ValueError` on anything unknown."""
    resolved = _resolve(enum_cls, value)
    if resolved is None:
        raise ValueError(
            f"invalid {field}: {value!r} (expected one of: {_valid_values(enum_cls)})"
        )
    return resolved
