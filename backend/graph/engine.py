"""
GraphEngine — facade that owns the per-project store cache and delegates all
heavy work to focused service classes:

  IndexerOrchestrator  — full index_repository pipeline
  QueryService         — search_graph, search_code, find_qualified_name, query_graph
  SnippetService       — get_code_snippet, trace_call_path, detect_changes
  ArchitectureService  — get_architecture, manage_adr
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from backend.graph.architecture_service import ArchitectureService
from backend.graph.indexer_orchestrator import IndexerOrchestrator
from backend.graph.query_service import QueryService
from backend.graph.snippet_service import SnippetService
from backend.graph.store import GraphStore

log = logging.getLogger(__name__)

_DB_DIR = ".waterfree"
_DB_FILE = "graph.db"


def _project_name(root_path: str) -> str:
    root = str(Path(root_path).resolve()).replace("\\", "/").rstrip("/")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", root).strip("-").lower() or "root"
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _db_path_for(root_path: str) -> str:
    return str(Path(root_path) / _DB_DIR / _DB_FILE)


class GraphEngine:
    """
    One GraphStore per project, opened lazily and cached for the server lifetime.
    DB lives at {workspace}/.waterfree/graph.db.
    """

    def __init__(self) -> None:
        self._stores: dict[str, tuple[GraphStore, str]] = {}
        self._db_stores: dict[str, GraphStore] = {}
        self._indexing: set[str] = set()
        self._discovered = False
        self._indexer = IndexerOrchestrator()
        self._query = QueryService()
        self._snippet = SnippetService()
        self._arch = ArchitectureService()

    # ------------------------------------------------------------------
    # Store access helpers
    # ------------------------------------------------------------------

    def _open_store(self, project: str, root_path: str) -> GraphStore:
        db_path = _db_path_for(root_path)
        store = self._db_stores.get(db_path)
        if store is None:
            store = GraphStore(db_path)
            self._db_stores[db_path] = store
        self._stores[project] = (store, root_path)
        return store

    def _load_store_from_db(self, db_path: Path) -> None:
        db_path_str = str(db_path)
        if db_path_str in self._db_stores:
            store = self._db_stores[db_path_str]
        else:
            store = GraphStore(db_path_str)
            self._db_stores[db_path_str] = store
        for project in store.list_projects():
            self._stores[project["name"]] = (store, project["root_path"])

    def _discover_stores(self) -> None:
        if self._discovered:
            return
        self._discovered = True
        cwd = Path.cwd()
        candidates = {cwd / _DB_DIR / _DB_FILE}
        candidates.update(cwd.rglob(f"{_DB_DIR}/{_DB_FILE}"))
        for candidate in sorted(candidates):
            if candidate.is_file():
                self._load_store_from_db(candidate)

    def _ensure_project_loaded(self, project: str) -> bool:
        if project in self._stores:
            return True
        self._discover_stores()
        return project in self._stores

    def _store(self, project: str) -> GraphStore:
        if self._ensure_project_loaded(project):
            return self._stores[project][0]
        raise RuntimeError(f"Project '{project}' has not been indexed yet")

    def _root(self, project: str) -> str:
        if self._ensure_project_loaded(project):
            return self._stores[project][1]
        return ""

    def _first_project(self) -> str:
        self._discover_stores()
        return sorted(self._stores)[0] if self._stores else ""

    def _resolve(self, project: str) -> str:
        if project:
            return project if self._ensure_project_loaded(project) else ""
        return self._first_project()

    # ------------------------------------------------------------------
    # index_repository — delegates to IndexerOrchestrator
    # ------------------------------------------------------------------

    def index_repository(self, repo_path: str) -> dict:
        root = str(Path(repo_path).resolve())
        project = _project_name(root)
        store = self._open_store(project, root)
        return self._indexer.index_repository(store, project, root, self._indexing)

    # ------------------------------------------------------------------
    # Project management
    # ------------------------------------------------------------------

    def list_projects(self) -> dict:
        self._discover_stores()
        projects: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for store, _ in self._stores.values():
            for project in store.list_projects():
                key = (project["name"], project["root_path"])
                if key in seen:
                    continue
                seen.add(key)
                project["db_path"] = store.db_path
                projects.append(project)
        projects.sort(key=lambda item: (item["root_path"], item["name"]))
        return {"projects": projects}

    def delete_project(self, project: str = "", repo_path: str = "") -> dict:
        if repo_path:
            project = _project_name(repo_path)
        project = self._resolve(project)
        if not project:
            return {"status": "not_found"}
        store = self._store(project)
        db_path = store.db_path
        root = self._root(project)
        store.delete_project(project)
        store.commit()
        self._stores.pop(project, None)
        self._indexing.discard(project)
        if store.project_count() == 0:
            store.delete_db_file()
            self._db_stores.pop(db_path, None)
        return {"status": "deleted", "project": project, "root_path": root, "db_path": db_path}

    def index_status(self, project: str = "", repo_path: str = "") -> dict:
        if repo_path:
            project = _project_name(repo_path)
        project = self._resolve(project)
        if not project:
            return {"status": "not_indexed", "project": ""}
        store = self._store(project)
        project_row = store.get_project(project)
        if not project_row:
            return {"status": "not_indexed", "project": project}
        status = "indexing" if project in self._indexing else "ready"
        return {
            "status": status,
            "project": project,
            "root_path": project_row["root_path"],
            "indexed_at": project_row["indexed_at"],
            "node_count": len(store.get_all_nodes(project)),
            "edge_count": store.count_edges(project),
            "db_path": store.db_path,
        }

    def get_graph_schema(self, project: str = "") -> dict:
        project = self._resolve(project)
        if not project:
            return {
                "project": "", "node_count": 0, "edge_count": 0,
                "node_labels": [], "edge_types": [], "relationship_patterns": [], "sample_nodes": [],
            }
        store = self._store(project)
        return {
            "project": project,
            "root_path": self._root(project),
            "db_path": store.db_path,
            "node_count": len(store.get_all_nodes(project)),
            "edge_count": store.count_edges(project),
            "node_labels": store.get_node_label_counts(project),
            "edge_types": store.get_edge_type_counts(project),
            "relationship_patterns": store.get_relationship_patterns(project),
            "sample_nodes": store.get_sample_nodes(project),
        }

    # ------------------------------------------------------------------
    # Architecture — delegates to ArchitectureService
    # ------------------------------------------------------------------

    def get_architecture(self, aspects: list[str] | None = None, project: str = "") -> dict:
        project = self._resolve(project)
        if not project:
            return {}
        return self._arch.get_architecture(self._store(project), project, self._root(project), aspects)

    def manage_adr(
        self, mode: str, content: str = "", sections: dict | None = None, project: str = ""
    ) -> dict:
        project = self._resolve(project)
        if not project:
            return {"error": "No project indexed"}
        return self._arch.manage_adr(self._store(project), project, mode, content, sections)

    # ------------------------------------------------------------------
    # Code snippets — delegates to SnippetService
    # ------------------------------------------------------------------

    def get_code_snippet(
        self,
        qualified_name: str,
        auto_resolve: bool = True,
        include_neighbors: bool = False,
        scope: str = "procedure",
        project: str = "",
    ) -> dict:
        project = self._resolve(project)
        if not project:
            return {}
        return self._snippet.get_code_snippet(
            self._store(project), project, qualified_name, auto_resolve, include_neighbors, scope
        )

    def trace_call_path(
        self,
        function_name: str,
        direction: str = "both",
        depth: int = 3,
        risk_labels: bool = False,
        min_confidence: float = 0.0,
        project: str = "",
    ) -> dict:
        project = self._resolve(project)
        if not project:
            return {"nodes": [], "edges": []}
        return self._snippet.trace_call_path(
            self._store(project), project, function_name, direction, depth, risk_labels, min_confidence
        )

    def detect_changes(self, scope: str = "all", depth: int = 3, project: str = "") -> dict:
        project = self._resolve(project)
        root = self._root(project)
        if not root:
            return {"changed_files": [], "changed_symbols": [], "impacted_callers": []}
        return self._snippet.detect_changes(self._store(project), project, root, scope, depth)

    # ------------------------------------------------------------------
    # Graph queries — delegates to QueryService
    # ------------------------------------------------------------------

    def search_graph(
        self,
        name_pattern: str = "",
        qn_pattern: str = "",
        file_pattern: str = "",
        label: str = "",
        relationship: str = "",
        direction: str = "any",
        min_degree: int = 0,
        max_degree: int = -1,
        limit: int = 10,
        offset: int = 0,
        case_sensitive: bool = False,
        project: str = "",
        **kwargs,
    ) -> dict:
        project = self._resolve(project)
        if not project:
            return {"results": [], "total": 0, "has_more": False, "limit": limit, "offset": offset}
        return self._query.search_graph(
            self._store(project), project, self._root(project),
            name_pattern, qn_pattern, file_pattern, label, relationship,
            direction, min_degree, max_degree, limit, offset, case_sensitive,
        )

    def search_code(
        self,
        pattern: str,
        regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 50,
        offset: int = 0,
        file_pattern: str = "",
        project: str = "",
    ) -> dict:
        project = self._resolve(project)
        root = self._root(project) or "."
        return self._query.search_code(root, pattern, regex, case_sensitive, max_results, offset, file_pattern)

    def find_qualified_name(self, short_name: str, project: str = "") -> str | None:
        project = self._resolve(project)
        if not project:
            return None
        return self._query.find_qualified_name(self._store(project), project, short_name)

    def query_graph(self, query: str, project: str = "") -> dict:
        project = self._resolve(project)
        if not project:
            return {"rows": []}
        return self._query.query_graph(self._store(project), project, query)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        for store in self._db_stores.values():
            store.close()
        self._stores.clear()
        self._db_stores.clear()
        self._indexing.clear()
        self._discovered = False
