"""
MCP server — knowledge / snippet store tools.

Exposes KnowledgeStore as MCP tools so Claude Code and other MCP clients can
search indexed code snippets, patterns, and conventions across all projects.

Run:
    python -m backend.mcp_knowledge

Register with Claude Code:
    claude mcp add pairprogram-knowledge python -- -m backend.mcp_knowledge
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from backend.mcp_logging import configure_mcp_logger, instrument_tool
from backend.knowledge.store import KnowledgeStore

mcp = FastMCP("pairprogram-knowledge")
log, LOG_FILE = configure_mcp_logger("pairprogram-knowledge")

# Single global store — knowledge.db is at ~/.waterfree/global/knowledge.db
_store: KnowledgeStore | None = None


def _get_store() -> KnowledgeStore:
    global _store
    if _store is None:
        _store = KnowledgeStore()
    return _store


def _entry_to_dict(entry) -> dict:
    return {
        "id": entry.id,
        "title": entry.title,
        "description": entry.description,
        "snippet_type": entry.snippet_type,
        "code": entry.code,
        "tags": entry.tags,
        "source_repo": entry.source_repo,
        "source_file": entry.source_file,
        "source_repo_url": entry.source_repo_url,
        "created_at": entry.created_at,
    }


def _repo_to_dict(repo) -> dict:
    return {
        "name": repo.name,
        "local_path": repo.local_path,
        "remote_url": repo.remote_url,
        "entry_count": repo.entry_count,
        "last_indexed": repo.last_indexed,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _search_knowledge_impl(query: str, limit: int = 10) -> str:
    """Search the global knowledge store for code snippets, patterns, and conventions.

    Uses BM25-ranked full-text search over title, description, tags, and code.

    Args:
        query: What to search for — e.g. 'authentication', 'retry pattern', 'singleton'.
        limit: Maximum number of results (default 10).

    Returns JSON list of matching knowledge entries with code, description, and metadata.
    """
    store = _get_store()
    entries = store.search(query, limit=limit)
    return json.dumps([_entry_to_dict(e) for e in entries], indent=2)


def _list_knowledge_sources_impl() -> str:
    """List all repositories and sources that have been indexed into the knowledge store.

    Returns JSON list of sources with name, path/URL, entry count, and last-indexed date.
    """
    store = _get_store()
    repos = store.list_repos()
    total = store.total_entries()
    return json.dumps(
        {
            "total_entries": total,
            "sources": [_repo_to_dict(r) for r in repos],
        },
        indent=2,
    )


def _knowledge_stats_impl() -> str:
    """Return summary statistics for the global knowledge store.

    Returns JSON with total entry count and number of indexed sources.
    """
    store = _get_store()
    repos = store.list_repos()
    return json.dumps(
        {
            "total_entries": store.total_entries(),
            "source_count": len(repos),
        },
        indent=2,
    )


search_knowledge = mcp.tool()(instrument_tool(log, "search_knowledge", _search_knowledge_impl))
list_knowledge_sources = mcp.tool()(
    instrument_tool(log, "list_knowledge_sources", _list_knowledge_sources_impl)
)
knowledge_stats = mcp.tool()(instrument_tool(log, "knowledge_stats", _knowledge_stats_impl))


if __name__ == "__main__":
    log.info("Starting MCP server pairprogram-knowledge (logFile=%s)", LOG_FILE)
    mcp.run()
