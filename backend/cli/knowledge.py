"""`waterfree knowledge ...` — global knowledge store CLI."""

from __future__ import annotations

import sys
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from backend.cli._common import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    emit_error,
    emit_json,
)
from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.store import KnowledgeStore


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("knowledge", help="Global knowledge / snippet store")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_search = actions.add_parser("search", help="Full-text search the knowledge store")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)

    p_browse = actions.add_parser("browse", help="Walk the knowledge taxonomy")
    p_browse.add_argument("--path", default="")
    p_browse.add_argument("--depth", type=int, default=2)
    p_browse.add_argument("--include-entries", action="store_true")
    p_browse.add_argument("--entry-limit", type=int, default=10)

    p_add = actions.add_parser("add", help="Add a knowledge entry")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--description", required=True)
    code_group = p_add.add_mutually_exclusive_group(required=True)
    code_group.add_argument("--code", help="Inline code body (small snippets only)")
    code_group.add_argument("--code-file",
                            help="Read code body from a file. Use '-' for stdin.")
    p_add.add_argument("--snippet-type", required=True,
                       choices=("pattern", "utility", "style", "api_usage", "convention"))
    p_add.add_argument("--source-repo", required=True)
    p_add.add_argument("--source-file", default="")
    p_add.add_argument("--tag", action="append", default=[],
                       help="Repeatable. e.g. --tag python --tag retry")
    p_add.add_argument("--context", default="")
    p_add.add_argument("--source-repo-url", default="")
    p_add.add_argument("--hierarchy-path", default="",
                       help="Slash-separated taxonomy path, e.g. platform/auth/jwt")

    p_delete = actions.add_parser("delete", help="Remove a knowledge entry by id")
    p_delete.add_argument("entry_id")

    actions.add_parser("list-sources", help="List all indexed repos/sources")
    actions.add_parser("stats", help="Summary statistics")

    p.set_defaults(_runner=run)


def _entry_to_dict(entry: KnowledgeEntry) -> dict:
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
        "hierarchy_path": entry.effective_hierarchy_path(),
        "hierarchy_segments": entry.effective_hierarchy_segments(),
        "hierarchy_source": entry.hierarchy_source(),
    }


def _repo_to_dict(repo) -> dict:
    return {
        "name": repo.name,
        "local_path": repo.local_path,
        "remote_url": repo.remote_url,
        "entry_count": repo.entry_count,
        "last_indexed": repo.last_indexed,
    }


def _read_code(args: Namespace) -> str:
    if args.code is not None:
        return args.code
    if args.code_file == "-":
        return sys.stdin.read()
    return Path(args.code_file).read_text(encoding="utf-8")


def run(args: Namespace) -> int:
    store = KnowledgeStore()
    action = args.action

    if action == "search":
        entries = store.search(args.query, limit=args.limit)
        emit_json([_entry_to_dict(e) for e in entries])
        return EXIT_OK

    if action == "browse":
        emit_json(store.browse_hierarchy(
            path=args.path,
            depth=args.depth,
            include_entries=args.include_entries,
            entry_limit=args.entry_limit,
        ))
        return EXIT_OK

    if action == "add":
        try:
            code = _read_code(args)
        except (OSError, FileNotFoundError) as exc:
            return emit_error(f"could not read code: {exc}", exit_code=EXIT_USAGE)
        entry = KnowledgeEntry.create(
            source_repo=args.source_repo,
            source_file=args.source_file,
            snippet_type=args.snippet_type,
            title=args.title,
            description=args.description,
            code=code,
            tags=args.tag,
            context=args.context,
            source_repo_url=args.source_repo_url,
            hierarchy_path=args.hierarchy_path or None,
        )
        added = store.add_entry(entry)
        store.upsert_repo(args.source_repo,
                          args.source_file or args.source_repo,
                          args.source_repo_url)
        emit_json({
            "id": entry.id,
            "added": added,
            "hierarchy_path": entry.effective_hierarchy_path(),
            "message": (
                f"Entry '{args.title}' added to knowledge store."
                if added
                else f"Entry '{args.title}' already exists (duplicate content — skipped)."
            ),
        })
        return EXIT_OK

    if action == "delete":
        deleted = store.delete_entry(args.entry_id)
        emit_json({
            "deleted": deleted,
            "message": (
                f"Entry {args.entry_id} deleted."
                if deleted
                else f"Entry {args.entry_id} not found."
            ),
        })
        return EXIT_OK if deleted else EXIT_NOT_FOUND

    if action == "list-sources":
        repos = store.list_repos()
        emit_json({
            "total_entries": store.total_entries(),
            "sources": [_repo_to_dict(r) for r in repos],
        })
        return EXIT_OK

    if action == "stats":
        repos = store.list_repos()
        hierarchy = store.browse_hierarchy(depth=1)
        emit_json({
            "total_entries": store.total_entries(),
            "source_count": len(repos),
            "top_level_category_count": len(hierarchy["nodes"]),
        })
        return EXIT_OK

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)
