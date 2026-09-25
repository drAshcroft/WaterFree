"""`waterfree index ...` — codebase graph CLI."""

from __future__ import annotations

import json
from typing import Any

from argparse import Namespace, _SubParsersAction

from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    add_workspace_arg,
    emit_error,
    emit_json,
    resolve_workspace,
)
from backend.graph.client import GraphClient


# Structural context earns its keep under a small token budget (Agent Retrieval
# Bench, 2026: repo-map style context led on yield at 8K tokens; CodeGrep, 2026:
# low-precision payloads cost more than they return). These actions therefore
# fit their output to an approximate token budget instead of dumping the whole
# graph, dropping the least important rows first and saying so.
_DEFAULT_ASPECTS = ("languages", "layers", "god_nodes")
_CHARS_PER_TOKEN = 4
# Per-payload list keys in the order they should be trimmed (least valuable first).
_TRIM_ORDER = {
    "trace": ("edges", "nodes"),
    "detect-changes": ("impacted_callers", "changed_symbols", "changed_files"),
    "architecture": ("module_graph", "clusters", "surprising_connections", "hotspots", "entry_points",
                     "import_cycles", "layers", "god_nodes", "languages"),
}


def _add_budget_flag(parser, default: int) -> None:
    parser.add_argument(
        "--budget-tokens", type=int, default=default,
        help=f"Approximate token budget for the output (default {default}); 0 = unlimited. "
             "Rows past the budget are dropped, least important first, and reported in `budget`.",
    )


def _estimate_tokens(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False)) // _CHARS_PER_TOKEN


def _rank_rows(rows: list) -> list:
    """Most important rows first, so trimming from the tail keeps the signal.

    Rows with a degree/score/confidence sort on it; rows without (a BFS trace,
    a changed-file list) keep their natural order, which is already nearest-first.
    """
    def key(row):
        if not isinstance(row, dict):
            return 0.0
        for field in ("degree", "score", "confidence", "count", "in_degree", "out_degree"):
            value = row.get(field)
            if isinstance(value, (int, float)):
                return -float(value)
        return 0.0
    if any(isinstance(r, dict) and any(k in r for k in ("degree", "score", "confidence", "count")) for r in rows):
        return sorted(rows, key=key)
    return list(rows)


def fit_budget(payload: dict, *, action: str, budget_tokens: int) -> dict:
    """Trim list-valued fields of `payload` until it fits `budget_tokens`.

    Returns the payload with a `budget` block: the budget, the estimated size
    before and after, and how many rows each list lost. A budget of 0 leaves
    the payload untouched (the block still reports the estimate).
    """
    if not isinstance(payload, dict):
        return payload
    before = _estimate_tokens(payload)
    dropped: dict[str, int] = {}
    if budget_tokens > 0 and before > budget_tokens:
        order = [k for k in _TRIM_ORDER.get(action, ()) if isinstance(payload.get(k), list)]
        order += [k for k, v in payload.items() if isinstance(v, list) and k not in order]
        for key in order:
            payload[key] = _rank_rows(payload[key])
        # Drop rows one at a time from the least valuable list; move to the
        # next list only once that one is empty, so the important lists keep
        # their rows for as long as possible.
        for key in order:
            rows = payload.get(key)
            while isinstance(rows, list) and rows and _estimate_tokens(payload) > budget_tokens:
                rows.pop()
                dropped[key] = dropped.get(key, 0) + 1
            if _estimate_tokens(payload) <= budget_tokens:
                break
    payload["budget"] = {
        "tokens": budget_tokens,
        "estimated_tokens_before": before,
        "estimated_tokens": _estimate_tokens(payload),
        "dropped": dropped,
        "truncated": bool(dropped),
    }
    if dropped:
        payload["budget"]["hint"] = (
            "Rows were dropped to fit --budget-tokens; raise it or narrow the query "
            "(smaller --depth, a specific --scope, fewer --aspect values)."
        )
    return payload


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("index", help="Codebase dependency graph")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_build = actions.add_parser("build", help="Index the workspace (full rebuild)")
    add_workspace_arg(p_build)

    p_status = actions.add_parser("status", help="Has the workspace been indexed?")
    add_workspace_arg(p_status)

    p_search_code = actions.add_parser("search-code", help="Find symbols by name")
    add_workspace_arg(p_search_code)
    p_search_code.add_argument("query")
    p_search_code.add_argument("--max", dest="max_results", type=int, default=20)

    p_search_graph = actions.add_parser("search-graph",
                                        help="Search graph nodes by qualified name")
    add_workspace_arg(p_search_graph)
    p_search_graph.add_argument("query")
    p_search_graph.add_argument("--node-type", default="")
    p_search_graph.add_argument("--limit", type=int, default=20)

    p_snippet = actions.add_parser("get-snippet", help="Fetch source for a symbol")
    add_workspace_arg(p_snippet)
    p_snippet.add_argument("qualified_name")
    p_snippet.add_argument("--scope", default="procedure",
                           choices=("procedure", "neighbors", "class"))

    p_trace = actions.add_parser("trace", help="Trace callers / callees")
    add_workspace_arg(p_trace)
    p_trace.add_argument("function")
    p_trace.add_argument("--direction", default="both",
                         choices=("callers", "callees", "both"))
    p_trace.add_argument("--depth", type=int, default=3)
    _add_budget_flag(p_trace, 1500)

    p_detect = actions.add_parser("detect-changes",
                                  help="Symbols affected by recent diff")
    add_workspace_arg(p_detect)
    p_detect.add_argument("--scope", default="all",
                          help="'all' (git diff) or comma-separated file paths")
    p_detect.add_argument("--depth", type=int, default=3)
    _add_budget_flag(p_detect, 1500)

    p_arch = actions.add_parser("architecture", help="High-level overview")
    add_workspace_arg(p_arch)
    p_arch.add_argument(
        "--aspect",
        default="",
        help="Comma-separated subset of: languages, entry_points, hotspots, layers, clusters, "
             "module_graph, adr, god_nodes, surprising_connections, import_cycles, all. "
             f"Default: {','.join(_DEFAULT_ASPECTS)} (the smallest useful overview).",
    )
    _add_budget_flag(p_arch, 2500)
    p_arch.add_argument("--all-aspects", action="store_true", help="Same as --aspect all.")

    p_god = actions.add_parser("god-nodes",
                               help="Most-connected symbols (core abstractions / refactor risks)")
    add_workspace_arg(p_god)
    p_god.add_argument("--limit", type=int, default=12)

    p_surprising = actions.add_parser("surprising",
                                      help="Non-obvious cross-layer / cross-language coupling")
    add_workspace_arg(p_surprising)
    p_surprising.add_argument("--limit", type=int, default=8)

    p_cycles = actions.add_parser("import-cycles", help="Circular import dependencies (file-level)")
    add_workspace_arg(p_cycles)

    p_clusters = actions.add_parser("clusters", help="Connected-component module clusters")
    add_workspace_arg(p_clusters)

    p_query = actions.add_parser("query", help="Run a pseudo-Cypher graph query")
    add_workspace_arg(p_query)
    p_query.add_argument("query")

    p_schema = actions.add_parser("schema",
                                  help="Node labels, edge types, and relationship patterns")
    add_workspace_arg(p_schema)

    actions.add_parser("list-projects", help="Projects in the global graph DB")

    p.set_defaults(_runner=run)


def _client_indexed(workspace_path: str) -> GraphClient:
    """Return a client that has already indexed the workspace."""
    client = GraphClient()
    status = client.index_status(repo_path=workspace_path)
    if status.get("status") != "ready":
        client.index(workspace_path)
    return client


def run(args: Namespace) -> int:
    action = args.action

    if action == "list-projects":
        emit_json(GraphClient().list_projects())
        return EXIT_OK

    workspace = resolve_workspace(args)

    if action == "build":
        emit_json(GraphClient().index(workspace))
        return EXIT_OK

    if action == "status":
        emit_json(GraphClient().index_status(repo_path=workspace))
        return EXIT_OK

    client = _client_indexed(workspace)

    if action == "search-code":
        emit_json(client.search_code(pattern=args.query, max_results=args.max_results))
        return EXIT_OK

    if action == "search-graph":
        kwargs: dict = {"query": args.query, "limit": args.limit}
        if args.node_type:
            kwargs["node_type"] = args.node_type
        emit_json(client.search_graph(**kwargs))
        return EXIT_OK

    if action == "get-snippet":
        emit_json(client.get_code_snippet(qualified_name=args.qualified_name, scope=args.scope))
        return EXIT_OK

    if action == "trace":
        result = client.trace_call_path(
            function_name=args.function,
            direction=args.direction,
            depth=args.depth,
        )
        emit_json(fit_budget(result, action="trace", budget_tokens=args.budget_tokens))
        return EXIT_OK

    if action == "detect-changes":
        result = client.detect_changes(scope=args.scope, depth=args.depth)
        emit_json(fit_budget(result, action="detect-changes", budget_tokens=args.budget_tokens))
        return EXIT_OK

    if action == "architecture":
        if args.all_aspects:
            aspects = ["all"]
        else:
            aspects = [a.strip() for a in args.aspect.split(",") if a.strip()] or list(_DEFAULT_ASPECTS)
        result = client.get_architecture(aspects=aspects)
        emit_json(fit_budget(result, action="architecture", budget_tokens=args.budget_tokens))
        return EXIT_OK

    if action == "god-nodes":
        arch = client.get_architecture(aspects=["god_nodes"])
        emit_json({"god_nodes": arch.get("god_nodes", [])[: args.limit]})
        return EXIT_OK

    if action == "surprising":
        arch = client.get_architecture(aspects=["surprising_connections"])
        emit_json({"surprising_connections": arch.get("surprising_connections", [])[: args.limit]})
        return EXIT_OK

    if action == "import-cycles":
        arch = client.get_architecture(aspects=["import_cycles"])
        emit_json({"import_cycles": arch.get("import_cycles", [])})
        return EXIT_OK

    if action == "clusters":
        arch = client.get_architecture(aspects=["clusters"])
        emit_json({"clusters": arch.get("clusters", [])})
        return EXIT_OK

    if action == "query":
        emit_json(client.query_graph(args.query))
        return EXIT_OK

    if action == "schema":
        emit_json(client.get_graph_schema())
        return EXIT_OK

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)
