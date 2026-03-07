"""
MCP server — codebase index / graph tools.

Exposes GraphClient as MCP tools so Claude Code and other MCP clients can
query the codebase dependency graph, search symbols, and detect change impact.

Run:
    python -m backend.mcp_index

Register with Claude Code:
    claude mcp add pairprogram-index python -- -m backend.mcp_index
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from backend.graph.client import GraphClient
from backend.mcp_logging import configure_mcp_logger, instrument_tool

mcp = FastMCP("pairprogram-index")
log, LOG_FILE = configure_mcp_logger("pairprogram-index")

# One GraphClient per workspace path, lazily created.
_clients: dict[str, GraphClient] = {}


def _client(workspace_path: str) -> GraphClient:
    if workspace_path not in _clients:
        _clients[workspace_path] = GraphClient()
    return _clients[workspace_path]


def _client_indexed(workspace_path: str) -> GraphClient:
    """Return a client that has already indexed the workspace."""
    client = _client(workspace_path)
    status = client.index_status(repo_path=workspace_path)
    if not status.get("indexed"):
        client.index(workspace_path)
    return client


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _index_workspace_impl(workspace_path: str) -> str:
    """Index a codebase directory into the dependency graph.

    Args:
        workspace_path: Absolute path to the project root.

    Returns JSON with node count, edge count, and project name.
    """
    client = _client(workspace_path)
    result = client.index(workspace_path)
    return json.dumps(result, indent=2)


def _search_code_impl(workspace_path: str, query: str, max_results: int = 20) -> str:
    """Search for symbols (functions, classes, variables) by name or keyword.

    Args:
        workspace_path: Absolute path to the project root (must be indexed).
        query: Search term or pattern.
        max_results: Maximum number of results to return (default 20).

    Returns JSON list of matching symbols with file paths and line numbers.
    """
    client = _client_indexed(workspace_path)
    result = client.search_code(pattern=query, max_results=max_results)
    return json.dumps(result, indent=2)


def _search_graph_impl(workspace_path: str, query: str, node_type: str = "", limit: int = 20) -> str:
    """Search the dependency graph by symbol name or qualified name.

    Args:
        workspace_path: Absolute path to the project root (must be indexed).
        query: Symbol name or partial qualified name to search for.
        node_type: Optional filter — e.g. 'function', 'class', 'module'.
        limit: Maximum number of results (default 20).

    Returns JSON list of matching graph nodes.
    """
    client = _client_indexed(workspace_path)
    kwargs: dict = {"query": query, "limit": limit}
    if node_type:
        kwargs["node_type"] = node_type
    result = client.search_graph(**kwargs)
    return json.dumps(result, indent=2)


def _get_code_snippet_impl(
    workspace_path: str,
    qualified_name: str,
    scope: str = "procedure",
) -> str:
    """Fetch the source code for a specific symbol by its qualified name.

    Args:
        workspace_path: Absolute path to the project root (must be indexed).
        qualified_name: Fully-qualified symbol name (e.g. 'mymodule.MyClass.method').
                        Partial names are auto-resolved.
        scope: Controls how much source is returned around the symbol.
               'procedure' — the symbol's own body only (default).
               'neighbors' — 20 lines above and below the symbol.
               'class'     — the full enclosing class body (falls back to
                             'procedure' if the symbol is not inside a class).

    Returns JSON with file path, line range, scope, and source code.
    """
    client = _client_indexed(workspace_path)
    result = client.get_code_snippet(qualified_name=qualified_name, scope=scope)
    return json.dumps(result, indent=2)


def _trace_call_path_impl(
    workspace_path: str,
    function_name: str,
    direction: str = "both",
    depth: int = 3,
) -> str:
    """Trace callers and/or callees of a function in the dependency graph.

    Args:
        workspace_path: Absolute path to the project root (must be indexed).
        function_name: Name or qualified name of the function to trace.
        direction: 'callers', 'callees', or 'both' (default 'both').
        depth: How many hops to traverse (default 3).

    Returns JSON call graph rooted at the given function.
    """
    client = _client_indexed(workspace_path)
    result = client.trace_call_path(
        function_name=function_name,
        direction=direction,
        depth=depth,
    )
    return json.dumps(result, indent=2)


def _detect_changes_impl(workspace_path: str, scope: str = "all", depth: int = 3) -> str:
    """Detect which symbols are affected by recent file changes.

    Args:
        workspace_path: Absolute path to the project root (must be indexed).
        scope: 'all' to check git diff, or a comma-separated list of file paths.
        depth: Impact propagation depth (default 3).

    Returns JSON describing changed files and their downstream impact radius.
    """
    client = _client_indexed(workspace_path)
    result = client.detect_changes(scope=scope, depth=depth)
    return json.dumps(result, indent=2)


def _get_architecture_impl(workspace_path: str) -> str:
    """Get a high-level architecture overview of the indexed project.

    Args:
        workspace_path: Absolute path to the project root (must be indexed).

    Returns JSON with modules, layers, key entry points, and dependency clusters.
    """
    client = _client_indexed(workspace_path)
    result = client.get_architecture()
    return json.dumps(result, indent=2)


def _index_status_impl(workspace_path: str) -> str:
    """Check whether a workspace has been indexed and how many nodes it has.

    Args:
        workspace_path: Absolute path to the project root.

    Returns JSON with 'indexed' boolean, node count, and last-indexed timestamp.
    """
    client = _client(workspace_path)
    result = client.index_status(repo_path=workspace_path)
    return json.dumps(result, indent=2)


def _list_projects_impl() -> str:
    """List all projects that have been indexed into the graph database.

    Returns JSON list of project names and their graph statistics.
    """
    # Use a throwaway client — list_projects is project-agnostic
    client = GraphClient()
    result = client.list_projects()
    return json.dumps(result, indent=2)


index_workspace = mcp.tool()(instrument_tool(log, "index_workspace", _index_workspace_impl))
search_code = mcp.tool()(instrument_tool(log, "search_code", _search_code_impl))
search_graph = mcp.tool()(instrument_tool(log, "search_graph", _search_graph_impl))
get_code_snippet = mcp.tool()(instrument_tool(log, "get_code_snippet", _get_code_snippet_impl))
trace_call_path = mcp.tool()(instrument_tool(log, "trace_call_path", _trace_call_path_impl))
detect_changes = mcp.tool()(instrument_tool(log, "detect_changes", _detect_changes_impl))
get_architecture = mcp.tool()(instrument_tool(log, "get_architecture", _get_architecture_impl))
index_status = mcp.tool()(instrument_tool(log, "index_status", _index_status_impl))
list_projects = mcp.tool()(instrument_tool(log, "list_projects", _list_projects_impl))


if __name__ == "__main__":
    log.info("Starting MCP server pairprogram-index (logFile=%s)", LOG_FILE)
    mcp.run()
