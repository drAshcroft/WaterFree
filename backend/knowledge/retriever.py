"""
Retriever — surfaces relevant global knowledge entries for prompt injection.

Called at context-build time to find the most relevant snippets for the
current session goal, then formats them as a compact markdown section.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.store import KnowledgeStore

log = logging.getLogger(__name__)

# Hard caps to prevent context window overflow
_MAX_ENTRIES = 8
_MAX_CODE_CHARS = 400   # truncate very long snippets
_MAX_TOTAL_CHARS = 2000 # abort adding more entries beyond this total


def search_for_context(
    query: str,
    store: Optional[KnowledgeStore] = None,
    limit: int = _MAX_ENTRIES,
) -> str:
    """
    Search the global knowledge base and return a formatted context section,
    or an empty string if the KB is empty or nothing is relevant.
    """
    _store = store or _get_default_store()
    if _store is None:
        return ""

    started = time.perf_counter()
    try:
        entries = _store.search(query, limit=limit)
    except Exception as exc:
        log.warning("knowledge retriever search failed: %s", exc)
        return ""

    section = _format_entries(entries) if entries else ""
    _log_injection(query, entries, section, (time.perf_counter() - started) * 1000.0)
    return section


def _log_injection(query: str, entries: list[KnowledgeEntry], section: str, duration_ms: float) -> None:
    """Count automatic prompt injection alongside explicit CLI searches.

    Records area=knowledge action=inject with source=extension, so
    `waterfree usage summary` shows how much of the store reaches a prompt
    without an agent ever asking for it.
    """
    try:
        from backend.cli import usage_log

        usage_log.append(usage_log.build_record(
            area="knowledge",
            action="inject",
            argv=[usage_log._clip(query)],
            workspace="",
            exit_code=0,
            duration_ms=duration_ms,
            result_bytes=len(section),
            payload={"entries": [{"id": e.id} for e in entries], "total": len(entries)},
            source="extension",
            agent="extension",
        ))
    except Exception:  # pragma: no cover
        pass


def _format_entries(entries: list[KnowledgeEntry]) -> str:
    lines: list[str] = ["GLOBAL KNOWLEDGE BASE (relevant patterns from other projects):"]
    total_chars = 0

    for entry in entries:
        code_preview = entry.code.strip()
        if len(code_preview) > _MAX_CODE_CHARS:
            code_preview = code_preview[:_MAX_CODE_CHARS] + "\n... (truncated)"

        block = (
            f"\n[{entry.snippet_type.upper()}: {entry.title}]\n"
            f"Source: {entry.source_repo} / {entry.source_file}\n"
            f"{entry.description}\n"
            f"```\n{code_preview}\n```"
        )

        if total_chars + len(block) > _MAX_TOTAL_CHARS:
            break

        lines.append(block)
        total_chars += len(block)

    if len(lines) == 1:
        # Only header — nothing fit
        return ""

    return "\n".join(lines)


# ------------------------------------------------------------------
# Singleton default store (lazy-init, None if DB not yet created)
# ------------------------------------------------------------------

_default_store: Optional[KnowledgeStore] = None


def _get_default_store() -> Optional[KnowledgeStore]:
    global _default_store
    if _default_store is not None:
        return _default_store

    from pathlib import Path
    db_path = Path.home() / ".waterfree" / "global" / "knowledge.db"
    if not db_path.exists():
        return None  # No KB yet — don't create an empty one on every call

    try:
        _default_store = KnowledgeStore()
        return _default_store
    except Exception as exc:
        log.warning("Could not open knowledge store: %s", exc)
        return None
