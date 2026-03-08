"""
Graph/index tool descriptors.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.graph.client import GraphClient

from .types import ToolDescriptor, ToolPolicy

log = logging.getLogger(__name__)


def _tool(name: str, description: str, schema: dict) -> tuple[str, str, dict]:
    return name, description, schema


def graph_tool_descriptors(graph: Optional[GraphClient]) -> list[ToolDescriptor]:
    if graph is None:
        return []

    def ensure_workspace(repo_path: str) -> None:
        if not repo_path:
            return
        try:
            graph.index_status(repo_path=repo_path)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("Graph workspace check failed for %s: %s", repo_path, exc)

    def index_repository(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        return graph.index(repo_path)

    def list_projects(_args: dict, _workspace_path: str) -> dict:
        return graph.list_projects()

    def index_status(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        return graph.index_status(project=str(args.get("project", "")), repo_path=repo_path)

    def get_graph_schema(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        ensure_workspace(repo_path)
        return graph.get_graph_schema(project=str(args.get("project", "")))

    def get_architecture(args: dict, workspace_path: str) -> dict:
        ensure_workspace(workspace_path)
        return graph.get_architecture(aspects=args.get("aspects"))

    def search_graph(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        ensure_workspace(repo_path)
        return graph.search_graph(
            name_pattern=str(args.get("namePattern", "")),
            qn_pattern=str(args.get("qnPattern", "")),
            file_pattern=str(args.get("filePattern", "")),
            label=str(args.get("label", "")),
            relationship=str(args.get("relationship", "")),
            direction=str(args.get("direction", "any")),
            min_degree=int(args.get("minDegree", 0)),
            max_degree=int(args.get("maxDegree", -1)),
            limit=int(args.get("limit", 10)),
            offset=int(args.get("offset", 0)),
            case_sensitive=bool(args.get("caseSensitive", False)),
            project=str(args.get("project", "")),
        )

    def search_code(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        ensure_workspace(repo_path)
        return graph.search_code(
            pattern=str(args.get("pattern", "")),
            regex=bool(args.get("regex", False)),
            case_sensitive=bool(args.get("caseSensitive", False)),
            max_results=int(args.get("maxResults", 50)),
            offset=int(args.get("offset", 0)),
            file_pattern=str(args.get("filePattern", "")),
        )

    def find_qualified_name(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        ensure_workspace(repo_path)
        return {"qualifiedName": graph.find_qualified_name(str(args.get("shortName", "")))}

    def get_code_snippet(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        ensure_workspace(repo_path)
        return graph.get_code_snippet(
            qualified_name=str(args.get("qualifiedName", "")),
            auto_resolve=bool(args.get("autoResolve", True)),
            include_neighbors=bool(args.get("includeNeighbors", False)),
        )

    def trace_call_path(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        ensure_workspace(repo_path)
        return graph.trace_call_path(
            function_name=str(args.get("functionName", "")),
            direction=str(args.get("direction", "both")),
            depth=int(args.get("depth", 3)),
            risk_labels=bool(args.get("riskLabels", False)),
            min_confidence=float(args.get("minConfidence", 0.0)),
        )

    def detect_changes(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        ensure_workspace(repo_path)
        return graph.detect_changes(
            scope=str(args.get("scope", "all")),
            depth=int(args.get("depth", 3)),
        )

    def query_graph(args: dict, workspace_path: str) -> dict:
        repo_path = str(args.get("repoPath", "") or workspace_path)
        ensure_workspace(repo_path)
        return graph.query_graph(str(args.get("query", "")))

    descriptors: list[ToolDescriptor] = []
    graph_policy = ToolPolicy(read_only=True, category="graph")
    graph_specs = [
        _tool(
            "index_repository",
            "Index or refresh a repository so graph queries use current source state.",
            {"type": "object", "properties": {"repoPath": {"type": "string"}}, "required": ["repoPath"]},
        ),
        _tool("list_projects", "List indexed graph projects.", {"type": "object", "properties": {}}),
        _tool(
            "index_status",
            "Get graph index readiness for a repository or project.",
            {"type": "object", "properties": {"project": {"type": "string"}, "repoPath": {"type": "string"}}},
        ),
        _tool(
            "get_graph_schema",
            "Inspect graph schema and relationship patterns.",
            {"type": "object", "properties": {"project": {"type": "string"}, "repoPath": {"type": "string"}}},
        ),
        _tool(
            "get_architecture",
            "Get architecture summaries such as entry points, hotspots, and layers.",
            {"type": "object", "properties": {"aspects": {"type": "array", "items": {"type": "string"}}}},
        ),
        _tool(
            "search_graph",
            "Find symbols in the indexed graph by name, qualified name, or file pattern.",
            {
                "type": "object",
                "properties": {
                    "namePattern": {"type": "string"},
                    "qnPattern": {"type": "string"},
                    "filePattern": {"type": "string"},
                    "label": {"type": "string"},
                    "relationship": {"type": "string"},
                    "direction": {"type": "string", "enum": ["any", "inbound", "outbound"]},
                    "minDegree": {"type": "integer"},
                    "maxDegree": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                    "caseSensitive": {"type": "boolean"},
                    "project": {"type": "string"},
                    "repoPath": {"type": "string"},
                },
            },
        ),
        _tool(
            "search_code",
            "Run indexed code search across workspace files.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "regex": {"type": "boolean"},
                    "caseSensitive": {"type": "boolean"},
                    "maxResults": {"type": "integer"},
                    "offset": {"type": "integer"},
                    "filePattern": {"type": "string"},
                    "repoPath": {"type": "string"},
                },
                "required": ["pattern"],
            },
        ),
        _tool(
            "find_qualified_name",
            "Resolve a short symbol name into a qualified name.",
            {
                "type": "object",
                "properties": {"shortName": {"type": "string"}, "repoPath": {"type": "string"}},
                "required": ["shortName"],
            },
        ),
        _tool(
            "get_code_snippet",
            "Fetch source and metadata for a symbol from the graph index.",
            {
                "type": "object",
                "properties": {
                    "qualifiedName": {"type": "string"},
                    "autoResolve": {"type": "boolean"},
                    "includeNeighbors": {"type": "boolean"},
                    "repoPath": {"type": "string"},
                },
                "required": ["qualifiedName"],
            },
        ),
        _tool(
            "trace_call_path",
            "Trace callers and callees for a function.",
            {
                "type": "object",
                "properties": {
                    "functionName": {"type": "string"},
                    "direction": {"type": "string", "enum": ["both", "inbound", "outbound"]},
                    "depth": {"type": "integer"},
                    "riskLabels": {"type": "boolean"},
                    "minConfidence": {"type": "number"},
                    "repoPath": {"type": "string"},
                },
                "required": ["functionName"],
            },
        ),
        _tool(
            "detect_changes",
            "Map changed files and symbols to impacted callers.",
            {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "depth": {"type": "integer"},
                    "repoPath": {"type": "string"},
                },
            },
        ),
        _tool(
            "query_graph",
            "Run a read-only graph query against indexed data.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}, "repoPath": {"type": "string"}},
                "required": ["query"],
            },
        ),
    ]
    handlers = {
        "index_repository": index_repository,
        "list_projects": list_projects,
        "index_status": index_status,
        "get_graph_schema": get_graph_schema,
        "get_architecture": get_architecture,
        "search_graph": search_graph,
        "search_code": search_code,
        "find_qualified_name": find_qualified_name,
        "get_code_snippet": get_code_snippet,
        "trace_call_path": trace_call_path,
        "detect_changes": detect_changes,
        "query_graph": query_graph,
    }
    for name, description, schema in graph_specs:
        descriptors.append(
            ToolDescriptor(
                name=name,
                title=name.replace("_", " "),
                description=description,
                input_schema=schema,
                handler=handlers[name],
                policy=graph_policy,
                server_id="waterfree-index",
            )
        )
    return descriptors
