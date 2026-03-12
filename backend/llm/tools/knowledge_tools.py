"""
Global knowledge/snippet tool descriptors.
"""

from __future__ import annotations

from typing import Callable

from backend.knowledge.store import KnowledgeStore

from .types import ToolDescriptor, ToolPolicy


def knowledge_tool_descriptors(
    knowledge_store_factory: Callable[[], KnowledgeStore],
) -> list[ToolDescriptor]:
    store_cache: KnowledgeStore | None = None

    def store() -> KnowledgeStore:
        nonlocal store_cache
        if store_cache is None:
            store_cache = knowledge_store_factory()
        return store_cache

    def search_knowledge(args: dict, _workspace_path: str) -> dict:
        knowledge = store()
        entries = knowledge.search(str(args.get("query", "")), limit=int(args.get("limit", 10)))
        return {
            "entries": [entry.to_dict() for entry in entries],
            "count": len(entries),
            "total": knowledge.total_entries(),
        }

    def list_knowledge_sources(_args: dict, _workspace_path: str) -> dict:
        knowledge = store()
        repos = knowledge.list_repos()
        return {
            "repos": [repo.to_dict() for repo in repos],
            "totalEntries": knowledge.total_entries(),
        }

    def browse_knowledge_index(args: dict, _workspace_path: str) -> dict:
        knowledge = store()
        return knowledge.browse_hierarchy(
            path=str(args.get("path", "")),
            depth=int(args.get("depth", 2)),
            include_entries=bool(args.get("includeEntries", False)),
            entry_limit=int(args.get("entryLimit", 10)),
        )

    return [
        ToolDescriptor(
            name="search_knowledge",
            title="search knowledge",
            description="Search the global snippet store for reusable patterns.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            },
            handler=search_knowledge,
            policy=ToolPolicy(read_only=True, category="knowledge"),
            server_id="waterfree-knowledge",
        ),
        ToolDescriptor(
            name="browse_knowledge_index",
            title="browse knowledge index",
            description="Browse the knowledge taxonomy when the topic is category-first or broad.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "depth": {"type": "integer"},
                    "includeEntries": {"type": "boolean"},
                    "entryLimit": {"type": "integer"},
                },
            },
            handler=browse_knowledge_index,
            policy=ToolPolicy(read_only=True, category="knowledge"),
            server_id="waterfree-knowledge",
        ),
        ToolDescriptor(
            name="list_knowledge_sources",
            title="list knowledge sources",
            description="List snippetized repositories available in the global knowledge store.",
            input_schema={"type": "object", "properties": {}},
            handler=list_knowledge_sources,
            policy=ToolPolicy(read_only=True, category="knowledge"),
            server_id="waterfree-knowledge",
        ),
    ]
