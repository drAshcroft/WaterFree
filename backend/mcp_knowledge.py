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
from backend.knowledge.models import KnowledgeEntry
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
        "context": entry.context,
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


def _add_knowledge_impl(
    title: str,
    description: str,
    code: str,
    snippet_type: str,
    source_repo: str,
    source_file: str = "",
    tags: list[str] | None = None,
    context: str = "",
    source_repo_url: str = "",
) -> str:
    """Add a knowledge entry directly to the global store.

    Use this when you discover a reusable pattern, utility, convention, or API usage
    that would benefit future coding sessions across projects. Always search first to
    avoid duplicates.

    Args:
        title: Short descriptive title (e.g. "Exponential backoff retry decorator").
        description: 2-4 sentence plain-English explanation of what this does and when to use it.
        code: The actual code content to store.
        snippet_type: One of: pattern, utility, style, api_usage, convention.
        source_repo: The project name or path this came from (e.g. "WaterFree", "c:/projects/myapp").
        source_file: Relative path of the source file within the repo (optional).
        tags: Relevant tags e.g. ["python", "async", "error-handling"] (optional).
        context: Extra context — caveats, version requirements, related files, when NOT to use (optional).
        source_repo_url: Git remote URL of the source repo (optional).

    Returns JSON with the new entry id and a confirmation message.
    """
    store = _get_store()
    entry = KnowledgeEntry.create(
        source_repo=source_repo,
        source_file=source_file,
        snippet_type=snippet_type,
        title=title,
        description=description,
        code=code,
        tags=tags or [],
        context=context,
        source_repo_url=source_repo_url,
    )
    added = store.add_entry(entry)
    store.upsert_repo(source_repo, source_file or source_repo, source_repo_url)
    return json.dumps(
        {
            "id": entry.id,
            "added": added,
            "message": (
                f"Entry '{title}' added to knowledge store."
                if added
                else f"Entry '{title}' already exists (duplicate content — skipped)."
            ),
        },
        indent=2,
    )


def _delete_knowledge_impl(entry_id: str) -> str:
    """Delete a knowledge entry from the global store by its ID.

    Use this to remove entries that are incorrect, outdated, or no longer relevant.
    The entry ID is returned by add_knowledge and search_knowledge.

    Args:
        entry_id: The UUID of the entry to delete.

    Returns JSON confirming deletion or noting the entry was not found.
    """
    store = _get_store()
    deleted = store.delete_entry(entry_id)
    return json.dumps(
        {
            "deleted": deleted,
            "message": (
                f"Entry {entry_id} deleted."
                if deleted
                else f"Entry {entry_id} not found."
            ),
        },
        indent=2,
    )


search_knowledge = mcp.tool()(instrument_tool(log, "search_knowledge", _search_knowledge_impl))
list_knowledge_sources = mcp.tool()(
    instrument_tool(log, "list_knowledge_sources", _list_knowledge_sources_impl)
)
knowledge_stats = mcp.tool()(instrument_tool(log, "knowledge_stats", _knowledge_stats_impl))
add_knowledge = mcp.tool()(instrument_tool(log, "add_knowledge", _add_knowledge_impl))
delete_knowledge = mcp.tool()(instrument_tool(log, "delete_knowledge", _delete_knowledge_impl))


if __name__ == "__main__":
    log.info("Starting MCP server pairprogram-knowledge (logFile=%s)", LOG_FILE)
    mcp.run()
