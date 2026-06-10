"""`waterfree index ...` — codebase graph CLI."""

from __future__ import annotations

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

    p_detect = actions.add_parser("detect-changes",
                                  help="Symbols affected by recent diff")
    add_workspace_arg(p_detect)
    p_detect.add_argument("--scope", default="all",
                          help="'all' (git diff) or comma-separated file paths")
    p_detect.add_argument("--depth", type=int, default=3)

    p_arch = actions.add_parser("architecture", help="High-level overview")
    add_workspace_arg(p_arch)
    p_arch.add_argument(
        "--aspect",
        default="",
        help="Comma-separated subset: languages, entry_points, hotspots, layers, "
             "clusters, module_graph, god_nodes, surprising_connections, "
             "import_cycles, adr. Default: all.",
    )

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
    if not status.get("indexed"):
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
        emit_json(client.trace_call_path(
            function_name=args.function,
            direction=args.direction,
            depth=args.depth,
        ))
        return EXIT_OK

    if action == "detect-changes":
        emit_json(client.detect_changes(scope=args.scope, depth=args.depth))
        return EXIT_OK

    if action == "architecture":
        aspects = [a.strip() for a in args.aspect.split(",") if a.strip()] or None
        emit_json(client.get_architecture(aspects=aspects))
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
