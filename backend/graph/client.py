"""
GraphClient — thin facade over GraphEngine.

Previously this spawned and communicated with an external codebase-memory-mcp
subprocess via MCP JSON-RPC. It now calls GraphEngine in-process with no
subprocess or network overhead.

The public API is unchanged so all callers (server.py, context_builder.py)
work without modification.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.graph.engine import GraphEngine

log = logging.getLogger(__name__)


class GraphClient:
    """
    Drop-in replacement for the subprocess-based GraphClient.
    All methods delegate directly to GraphEngine.
    Each project's graph is stored in {workspace}/.waterfree/graph.db.
    """

    def __init__(self, binary: str = ""):
        # `binary` param kept for API compatibility; ignored.
        self._engine = GraphEngine()
        self._project: Optional[str] = None

    # ------------------------------------------------------------------
    # Core lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._engine.close()

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def index(self, repo_path: str) -> dict:
        result = self._engine.index_repository(repo_path)
        self._project = result.get("project", "")
        log.info("GraphClient: indexed project %r — %d nodes", self._project, result.get("nodes", 0))
        return result

    def list_projects(self) -> dict:
        return self._engine.list_projects()

    def delete_project(self, project: str = "", repo_path: str = "") -> dict:
        result = self._engine.delete_project(project=project, repo_path=repo_path)
        deleted_project = result.get("project")
        if deleted_project and self._project == deleted_project:
            self._project = None
        return result

    def index_status(self, project: str = "", repo_path: str = "") -> dict:
        status = self._engine.index_status(project=project, repo_path=repo_path)
        if status.get("project"):
            self._project = str(status["project"])
        return status

    def get_graph_schema(self, project: str = "") -> dict:
        return self._engine.get_graph_schema(project=project or self._project or "")

    # ------------------------------------------------------------------
    # Architecture
    # ------------------------------------------------------------------

    def get_architecture(self, aspects: list[str] | None = None) -> dict:
        return self._engine.get_architecture(aspects=aspects, project=self._project or "")

    # ------------------------------------------------------------------
    # Call tracing
    # ------------------------------------------------------------------

    def trace_call_path(
        self,
        function_name: str,
        direction: str = "both",
        depth: int = 3,
        risk_labels: bool = False,
        min_confidence: float = 0.0,
    ) -> dict:
        return self._engine.trace_call_path(
            function_name=function_name,
            direction=direction,
            depth=depth,
            risk_labels=risk_labels,
            min_confidence=min_confidence,
            project=self._project or "",
        )

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def detect_changes(self, scope: str = "all", depth: int = 3) -> dict:
        return self._engine.detect_changes(scope=scope, depth=depth, project=self._project or "")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_graph(self, **kwargs) -> dict:
        return self._engine.search_graph(project=self._project or "", **kwargs)

    def search_code(
        self,
        pattern: str,
        regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 50,
        offset: int = 0,
        file_pattern: str = "",
    ) -> dict:
        return self._engine.search_code(
            pattern=pattern,
            regex=regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
            offset=offset,
            file_pattern=file_pattern,
            project=self._project or "",
        )

    def find_qualified_name(self, short_name: str) -> Optional[str]:
        return self._engine.find_qualified_name(short_name, project=self._project or "")

    # ------------------------------------------------------------------
    # Code inspection
    # ------------------------------------------------------------------

    def get_code_snippet(
        self,
        qualified_name: str,
        auto_resolve: bool = True,
        include_neighbors: bool = False,
        scope: str = "procedure",
    ) -> dict:
        return self._engine.get_code_snippet(
            qualified_name=qualified_name,
            auto_resolve=auto_resolve,
            include_neighbors=include_neighbors,
            scope=scope,
            project=self._project or "",
        )

    def query_graph(self, query: str) -> dict:
        return self._engine.query_graph(query=query, project=self._project or "")

    # ------------------------------------------------------------------
    # ADR
    # ------------------------------------------------------------------

    def manage_adr(self, mode: str, **kwargs) -> dict:
        return self._engine.manage_adr(mode=mode, project=self._project or "", **kwargs)
