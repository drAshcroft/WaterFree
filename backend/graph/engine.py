"""
GraphEngine — internal Python replacement for the codebase-memory-mcp Go binary.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import subprocess
from collections import deque
from pathlib import Path
from typing import Pattern

from backend.graph.indexer import IndexPipeline, collect_files, resolve_calls
from backend.graph.store import GraphStore

log = logging.getLogger(__name__)

_DB_DIR = ".waterfree"
_DB_FILE = "graph.db"

_RISK = {0: "CRITICAL", 1: "HIGH", 2: "MEDIUM", 3: "LOW"}


def _project_name(root_path: str) -> str:
    root = str(Path(root_path).resolve()).replace("\\", "/").rstrip("/")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", root).strip("-").lower() or "root"
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _db_path_for(root_path: str) -> str:
    return str(Path(root_path) / _DB_DIR / _DB_FILE)


def _compile_pattern(pattern: str, case_sensitive: bool) -> Pattern[str] | None:
    if not pattern:
        return None
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error:
        return re.compile(re.escape(pattern), flags)


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

    # ------------------------------------------------------------------
    # Store access helpers
    # ------------------------------------------------------------------

    def _open_store(self, project: str, root_path: str) -> GraphStore:
        """Open (or return cached) store for a project."""
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
        """Return the store for an already-opened project."""
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
        """Return project if given; otherwise the first open/discovered project."""
        if project:
            return project if self._ensure_project_loaded(project) else ""
        return self._first_project()

    def _cleanup_deleted_files(
        self,
        store: GraphStore,
        project: str,
        root: str,
        stored_hashes: dict[str, str],
        current_rels: list[str],
    ) -> int:
        deleted = sorted(set(stored_hashes) - set(current_rels))
        for rel_path in deleted:
            abs_path = str(Path(root) / rel_path)
            store.delete_edges_for_file(project, abs_path)
            store.delete_nodes_for_file(project, abs_path)
            store.delete_file_hash(project, rel_path)
        if deleted:
            store.commit()
        return len(deleted)

    # ------------------------------------------------------------------
    # index_repository
    # ------------------------------------------------------------------

    def index_repository(self, repo_path: str) -> dict:
        root = str(Path(repo_path).resolve())
        project = _project_name(root)
        store = self._open_store(project, root)

        self._indexing.add(project)
        try:
            for existing in store.list_projects():
                if existing["root_path"] == root and existing["name"] != project:
                    log.info(
                        "Removing legacy project entry %r for root %s",
                        existing["name"],
                        root,
                    )
                    store.delete_project(existing["name"])

            store.upsert_project(project, root)
            store.commit()

            stored_hashes = store.get_file_hashes(project)
            pipeline = IndexPipeline(project, root)
            result = pipeline.run(stored_hashes)
            parsed = result["parsed"]

            deleted_files = self._cleanup_deleted_files(
                store,
                project,
                root,
                stored_hashes,
                result.get("all_rels", []),
            )

            if not parsed:
                return {
                    "project": project,
                    "files_indexed": 0,
                    "deleted_files": deleted_files,
                    "nodes": len(store.get_all_nodes(project)),
                    "edges": store.count_edges(project),
                    "status": "up_to_date",
                }

            name_to_qns: dict[str, list[str]] = {}
            for node in store.get_all_nodes(project):
                name_to_qns.setdefault(node["name"], []).append(node["qualified_name"])

            for parsed_file in parsed:
                store.delete_edges_for_file(project, parsed_file.path)
                store.delete_nodes_for_file(project, parsed_file.path)

                mod_qn = parsed_file.module_qn_prefix
                store.upsert_node(
                    project,
                    "Module",
                    Path(parsed_file.path).stem,
                    mod_qn,
                    parsed_file.path,
                    1,
                    1,
                    {"language": parsed_file.lang},
                )

                for sym in parsed_file.symbols:
                    store.upsert_node(
                        project,
                        sym.label,
                        sym.name,
                        sym.qualified_name,
                        parsed_file.path,
                        sym.start_line,
                        sym.end_line,
                        {**sym.properties, "body_snippet": sym.body[:200]},
                    )
                    name_to_qns.setdefault(sym.name, []).append(sym.qualified_name)

            store.commit()

            for parsed_file in parsed:
                for src_qn, tgt_qn in resolve_calls(parsed_file, name_to_qns):
                    source = store.get_node_by_qn(project, src_qn)
                    target = store.get_node_by_qn(project, tgt_qn)
                    if source and target:
                        store.upsert_edge(
                            project,
                            source["id"],
                            target["id"],
                            "CALLS",
                            {"confidence": 0.6},
                        )

                mod_node = store.get_node_by_qn(project, parsed_file.module_qn_prefix)
                if mod_node:
                    for sym in parsed_file.symbols:
                        sym_node = store.get_node_by_qn(project, sym.qualified_name)
                        if sym_node:
                            store.upsert_edge(
                                project,
                                mod_node["id"],
                                sym_node["id"],
                                "DEFINES",
                            )

                for sym in parsed_file.symbols:
                    if sym.label != "Class":
                        continue
                    sym_node = store.get_node_by_qn(project, sym.qualified_name)
                    if not sym_node:
                        continue
                    for base in sym.properties.get("base_classes", []):
                        for base_node in store.find_nodes_by_name(project, base, "Class")[:1]:
                            store.upsert_edge(
                                project,
                                sym_node["id"],
                                base_node["id"],
                                "INHERITS",
                            )

            store.commit()

            for rel_path, sha in result["new_hashes"].items():
                store.upsert_file_hash(project, rel_path, sha)
            store.commit()

            return {
                "project": project,
                "files_indexed": result["files_indexed"],
                "deleted_files": deleted_files,
                "nodes": len(store.get_all_nodes(project)),
                "edges": store.count_edges(project),
                "status": "indexed",
            }
        finally:
            self._indexing.discard(project)

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

        return {
            "status": "deleted",
            "project": project,
            "root_path": root,
            "db_path": db_path,
        }

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

    # ------------------------------------------------------------------
    # get_graph_schema
    # ------------------------------------------------------------------

    def get_graph_schema(self, project: str = "") -> dict:
        project = self._resolve(project)
        if not project:
            return {
                "project": "",
                "node_count": 0,
                "edge_count": 0,
                "node_labels": [],
                "edge_types": [],
                "relationship_patterns": [],
                "sample_nodes": [],
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
    # get_architecture
    # ------------------------------------------------------------------

    def get_architecture(self, aspects: list[str] | None = None, project: str = "") -> dict:
        project = self._resolve(project)
        if not project:
            return {}
        store = self._store(project)
        aspects = aspects or ["all"]
        out: dict = {}

        if "all" in aspects or "languages" in aspects:
            out["languages"] = self._languages(store, project)
        if "all" in aspects or "entry_points" in aspects:
            out["entry_points"] = self._entry_points(store, project)
        if "all" in aspects or "hotspots" in aspects:
            out["hotspots"] = self._hotspots(store, project)
        if "all" in aspects or "layers" in aspects:
            out["layers"] = self._layers(store, project)
        if "all" in aspects or "clusters" in aspects:
            out["clusters"] = self._clusters(store, project)
        if "all" in aspects or "adr" in aspects:
            text = store.get_summary(project)
            if text:
                out["adr"] = {"text": text}

        return out

    def _languages(self, store: GraphStore, project: str) -> list[dict]:
        counts: dict[str, int] = {}
        for module in store.get_all_nodes(project, "Module"):
            try:
                lang = json.loads(module.get("properties") or "{}").get("language", "unknown")
            except Exception:
                lang = "unknown"
            counts[lang] = counts.get(lang, 0) + 1
        return [
            {"name": name, "file_count": count}
            for name, count in sorted(counts.items(), key=lambda item: -item[1])
        ]

    def _entry_points(self, store: GraphStore, project: str) -> list[dict]:
        names = {"main", "index", "app", "server", "extension", "__main__"}
        return [
            {
                "name": node["name"],
                "qualified_name": node["qualified_name"],
                "file_path": node["file_path"],
                "label": node["label"],
            }
            for node in store.get_all_nodes(project)
            if node["name"].lower() in names
        ][:10]

    def _hotspots(self, store: GraphStore, project: str) -> list[dict]:
        scored = []
        for node in store.get_all_nodes(project):
            if node["label"] not in ("Function", "Method"):
                continue
            in_degree = store.get_in_degree(node["id"], "CALLS")
            if in_degree > 0:
                scored.append(
                    {
                        "name": node["name"],
                        "qualified_name": node["qualified_name"],
                        "file_path": node["file_path"],
                        "in_degree": in_degree,
                    }
                )
        scored.sort(key=lambda item: -item["in_degree"])
        return scored[:20]

    def _layers(self, store: GraphStore, project: str) -> list[dict]:
        root = self._root(project)
        layer_map: dict[str, list[str]] = {}
        for module in store.get_all_nodes(project, "Module"):
            file_path = module["file_path"] or ""
            try:
                rel = str(Path(file_path).relative_to(root))
            except ValueError:
                rel = file_path
            layer = Path(rel).parts[0] if len(Path(rel).parts) > 1 else "(root)"
            layer_map.setdefault(layer, []).append(module["name"])
        return [
            {"name": layer, "modules": names[:5], "file_count": len(names)}
            for layer, names in sorted(layer_map.items())
        ]

    def _clusters(self, store: GraphStore, project: str) -> list[dict]:
        nodes = store.get_all_nodes(project)
        if not nodes:
            return []

        adjacency: dict[int, set[int]] = {node["id"]: set() for node in nodes}
        for node in nodes:
            for edge in store.get_outbound_edges(project, node["id"], "CALLS"):
                adjacency[node["id"]].add(edge["target_id"])

        id_to_node = {node["id"]: node for node in nodes}
        visited: set[int] = set()
        clusters: list[list[int]] = []
        for node_id in adjacency:
            if node_id in visited:
                continue
            component: list[int] = []
            queue = deque([node_id])
            while queue:
                current = queue.popleft()
                if current in visited:
                    continue
                visited.add(current)
                component.append(current)
                queue.extend(neighbor for neighbor in adjacency.get(current, set()) if neighbor not in visited)
            if len(component) > 1:
                clusters.append(component)

        clusters.sort(key=lambda comp: -len(comp))
        return [
            {
                "id": index,
                "size": len(component),
                "members": [
                    {
                        "name": id_to_node[node_id]["name"],
                        "qualified_name": id_to_node[node_id]["qualified_name"],
                    }
                    for node_id in component[:8]
                    if node_id in id_to_node
                ],
            }
            for index, component in enumerate(clusters[:10])
        ]

    # ------------------------------------------------------------------
    # trace_call_path
    # ------------------------------------------------------------------

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
        store = self._store(project)

        starts = store.find_nodes_by_name(project, function_name) or store.search_nodes(
            project,
            function_name,
            limit=3,
        )
        if not starts:
            return {"nodes": [], "edges": []}

        start_id = starts[0]["id"]
        visited: dict[int, int] = {}
        path_nodes: list[dict] = []
        path_edges: list[dict] = []

        def bfs(start: int, outbound: bool) -> None:
            queue: deque[tuple[int, int]] = deque([(start, 0)])
            while queue:
                cur_id, dist = queue.popleft()
                if cur_id in visited or dist > depth:
                    continue
                visited[cur_id] = dist
                node = store.get_node_by_id(cur_id)
                if node:
                    entry = dict(node)
                    if risk_labels:
                        entry["risk"] = _RISK.get(dist, "LOW")
                    path_nodes.append(entry)
                edges = (
                    store.get_outbound_edges(project, cur_id, "CALLS")
                    if outbound
                    else store.get_inbound_edges(project, cur_id, "CALLS")
                )
                for edge in edges:
                    try:
                        confidence = json.loads(edge.get("properties") or "{}").get("confidence", 1.0)
                    except Exception:
                        confidence = 1.0
                    if confidence < min_confidence:
                        continue
                    next_id = edge["target_id"] if outbound else edge["source_id"]
                    path_edges.append(
                        {
                            "source": cur_id,
                            "target": next_id,
                            "type": "CALLS",
                            "confidence": confidence,
                        }
                    )
                    if next_id not in visited:
                        queue.append((next_id, dist + 1))

        if direction in ("outbound", "both"):
            bfs(start_id, outbound=True)
        if direction in ("inbound", "both"):
            bfs(start_id, outbound=False)

        critical = sum(1 for node in path_nodes if node.get("risk") == "CRITICAL")
        high = sum(1 for node in path_nodes if node.get("risk") == "HIGH")
        impact = (
            f"{len(path_nodes)} nodes affected ({critical} CRITICAL, {high} HIGH)"
            if risk_labels
            else ""
        )
        return {"nodes": path_nodes, "edges": path_edges, "impact_summary": impact}

    # ------------------------------------------------------------------
    # detect_changes
    # ------------------------------------------------------------------

    def detect_changes(self, scope: str = "all", depth: int = 3, project: str = "") -> dict:
        project = self._resolve(project)
        root = self._root(project)
        if not root:
            return {"changed_files": [], "changed_symbols": [], "impacted_callers": []}
        store = self._store(project)

        changed_files = self._git_changed_files(root, scope)
        if not changed_files:
            return {"changed_files": [], "changed_symbols": [], "impacted_callers": []}

        changed_symbols: list[dict] = []
        all_nodes = store.get_all_nodes(project)
        for file_path in changed_files:
            for node in all_nodes:
                if node.get("file_path") == file_path:
                    changed_symbols.append(
                        {
                            "name": node["name"],
                            "qualified_name": node["qualified_name"],
                            "file_path": node["file_path"],
                            "label": node["label"],
                            "risk": "HIGH",
                        }
                    )

        seen_ids: set[int] = set()
        impacted: list[dict] = []
        for symbol in changed_symbols:
            bfs_result = self.trace_call_path(
                symbol["name"],
                direction="inbound",
                depth=depth,
                risk_labels=True,
                project=project,
            )
            for caller in bfs_result.get("nodes", []):
                caller_id = caller.get("id")
                if caller_id and caller_id not in seen_ids:
                    seen_ids.add(caller_id)
                    impacted.append(caller)

        return {
            "changed_files": changed_files,
            "changed_symbols": changed_symbols,
            "impacted_callers": impacted,
        }

    def _git_changed_files(self, root: str, scope: str) -> list[str]:
        try:
            if scope == "staged":
                cmd = ["git", "diff", "--cached", "--name-only"]
            elif scope == "unstaged":
                cmd = ["git", "diff", "--name-only"]
            else:
                cmd = ["git", "diff", "HEAD", "--name-only"]
            output = subprocess.check_output(cmd, cwd=root, text=True, stderr=subprocess.DEVNULL)
            return [str(Path(root) / file_name) for file_name in output.strip().splitlines() if file_name]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # search_graph
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
        store = self._store(project)

        root = self._root(project)
        name_rx = _compile_pattern(name_pattern, case_sensitive)
        qn_rx = _compile_pattern(qn_pattern, case_sensitive)
        degree_edge_type = relationship or "CALLS"

        nodes = store.get_all_nodes(project, label=label)
        results = []
        for node in nodes:
            if name_rx and not name_rx.search(node["name"]):
                continue
            if qn_rx and not qn_rx.search(node["qualified_name"]):
                continue

            file_path = node["file_path"] or ""
            if file_pattern:
                rel_path = file_path
                if root and file_path:
                    try:
                        rel_path = str(Path(file_path).relative_to(root))
                    except ValueError:
                        rel_path = file_path
                if not fnmatch.fnmatch(rel_path, file_pattern) and not fnmatch.fnmatch(file_path, file_pattern):
                    continue

            in_degree = store.get_in_degree(node["id"], degree_edge_type)
            out_degree = store.get_out_degree(node["id"], degree_edge_type)
            if direction == "inbound":
                degree = in_degree
            elif direction == "outbound":
                degree = out_degree
            else:
                degree = max(in_degree, out_degree)

            if min_degree > 0 and degree < min_degree:
                continue
            if max_degree >= 0 and degree > max_degree:
                continue

            results.append(
                {
                    "name": node["name"],
                    "qualified_name": node["qualified_name"],
                    "label": node["label"],
                    "file_path": node["file_path"],
                    "start_line": node["start_line"],
                    "in_degree": in_degree,
                    "out_degree": out_degree,
                }
            )

        results.sort(key=lambda item: (item["name"].lower(), item["qualified_name"].lower()))
        total = len(results)
        page = results[offset: offset + limit]
        return {
            "results": page,
            "total": total,
            "has_more": offset + limit < total,
            "limit": limit,
            "offset": offset,
        }

    # ------------------------------------------------------------------
    # search_code
    # ------------------------------------------------------------------

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

        flags = re.MULTILINE | (0 if case_sensitive else re.IGNORECASE)
        try:
            search_re = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as exc:
            return {"matches": [], "total": 0, "error": str(exc), "has_more": False}

        all_matches: list[dict] = []
        for file_path in collect_files(root):
            rel_path = str(file_path.relative_to(root)) if Path(root) in file_path.parents else str(file_path)
            if file_pattern and not fnmatch.fnmatch(rel_path, file_pattern) and not fnmatch.fnmatch(str(file_path), file_pattern):
                continue

            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            lines = text.splitlines()
            for match in search_re.finditer(text):
                line_no = text[:match.start()].count("\n") + 1
                all_matches.append(
                    {
                        "file": str(file_path),
                        "line": line_no,
                        "text": lines[line_no - 1].strip() if line_no <= len(lines) else "",
                        "match": match.group(0),
                    }
                )

        total = len(all_matches)
        page = all_matches[offset: offset + max_results]
        return {
            "matches": page,
            "total": total,
            "has_more": offset + max_results < total,
            "limit": max_results,
            "offset": offset,
        }

    # ------------------------------------------------------------------
    # get_code_snippet
    # ------------------------------------------------------------------

    def get_code_snippet(
        self,
        qualified_name: str,
        auto_resolve: bool = True,
        include_neighbors: bool = False,
        scope: str = "procedure",
        project: str = "",
    ) -> dict:
        """Fetch source for a symbol.

        scope:
          'procedure' — the symbol's own body (default).
          'neighbors' — 20 lines above and below the symbol.
          'class'     — the enclosing class body (falls back to procedure if
                        the symbol is not inside a class).
        """
        project = self._resolve(project)
        if not project:
            return {}
        store = self._store(project)

        node = store.get_node_by_qn(project, qualified_name)
        if not node and auto_resolve:
            short = qualified_name.split(".")[-1]
            candidates = store.find_nodes_by_name(project, short)
            node = candidates[0] if candidates else None
        if not node:
            return {"error": f"Symbol not found: {qualified_name}"}

        try:
            props = json.loads(node.get("properties") or "{}")
        except Exception:
            props = {}

        read_start = node["start_line"]
        read_end = node["end_line"]

        if scope == "neighbors":
            read_start = max(1, node["start_line"] - 20)
            read_end = node["end_line"] + 20
        elif scope == "class":
            enclosing = self._find_enclosing_class(store, project, node)
            if enclosing:
                read_start = enclosing["start_line"]
                read_end = enclosing["end_line"]

        result = {
            "name": node["name"],
            "qualified_name": node["qualified_name"],
            "label": node["label"],
            "file_path": node["file_path"],
            "start_line": node["start_line"],
            "end_line": node["end_line"],
            "scope": scope,
            "source_start_line": read_start,
            "source_end_line": read_end,
            "source": self._read_lines(node["file_path"], read_start, read_end),
            "signature": props.get("signature", ""),
        }
        if include_neighbors:
            result["callers"] = [
                {"name": edge["name"], "qualified_name": edge["qualified_name"]}
                for edge in store.get_inbound_edges(project, node["id"], "CALLS")[:10]
            ]
            result["callees"] = [
                {"name": edge["name"], "qualified_name": edge["qualified_name"]}
                for edge in store.get_outbound_edges(project, node["id"], "CALLS")[:10]
            ]
        return result

    def _find_enclosing_class(self, store: "GraphStore", project: str, node: dict) -> dict | None:
        """Return the Class node that contains *node* in the same file, or None."""
        file_path = node.get("file_path")
        if not file_path:
            return None
        sym_start = node["start_line"]
        sym_end = node["end_line"]
        best: dict | None = None
        best_size = float("inf")
        for candidate in store.get_all_nodes(project, label="Class"):
            if candidate.get("file_path") != file_path:
                continue
            c_start = candidate["start_line"]
            c_end = candidate["end_line"]
            if c_start <= sym_start and c_end >= sym_end:
                size = c_end - c_start
                if size < best_size:
                    best = candidate
                    best_size = size
        return best

    def _read_lines(self, file_path: str, start: int, end: int) -> str:
        if not file_path:
            return ""
        try:
            lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
            start_idx = max(0, start - 1)
            end_idx = min(len(lines), end if end > 0 else start + 30)
            return "\n".join(lines[start_idx:end_idx])
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # find_qualified_name
    # ------------------------------------------------------------------

    def find_qualified_name(self, short_name: str, project: str = "") -> str | None:
        project = self._resolve(project)
        if not project:
            return None
        store = self._store(project)
        candidates = store.find_nodes_by_name(project, short_name)
        if candidates:
            return candidates[0]["qualified_name"]
        nodes = store.search_nodes(project, short_name, limit=5)
        return nodes[0]["qualified_name"] if nodes else None

    # ------------------------------------------------------------------
    # query_graph
    # ------------------------------------------------------------------

    def query_graph(self, query: str, project: str = "") -> dict:
        project = self._resolve(project)
        if not project:
            return {"rows": []}
        store = self._store(project)

        label_match = re.search(r"\([\w]+:([\w]+)\)", query)
        label = label_match.group(1) if label_match else ""
        name_regex = re.search(r"name\s*=~\s*['\"]([^'\"]+)['\"]", query)
        name_equals = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", query)

        if name_regex:
            nodes = store.search_nodes(project, name_regex.group(1).lstrip("(?i)"), label=label, limit=50)
        elif name_equals:
            nodes = store.find_nodes_by_name(project, name_equals.group(1), label=label)
        else:
            nodes = store.get_all_nodes(project, label=label)[:50]

        if "CALLS" in query and "->" in query:
            rows = []
            for node in nodes[:20]:
                for edge in store.get_outbound_edges(project, node["id"], "CALLS"):
                    rows.append(
                        {
                            "source": node["name"],
                            "source_qn": node["qualified_name"],
                            "target": edge["name"],
                            "target_qn": edge["qualified_name"],
                            "type": "CALLS",
                        }
                    )
            return {"rows": rows[:200]}

        return {
            "rows": [
                {
                    "name": node["name"],
                    "qualified_name": node["qualified_name"],
                    "label": node["label"],
                    "file_path": node["file_path"],
                }
                for node in nodes
            ]
        }

    # ------------------------------------------------------------------
    # manage_adr
    # ------------------------------------------------------------------

    def manage_adr(
        self,
        mode: str,
        content: str = "",
        sections: dict | None = None,
        project: str = "",
    ) -> dict:
        project = self._resolve(project)
        if not project:
            return {"error": "No project indexed"}
        store = self._store(project)

        if mode == "get":
            return {"text": store.get_summary(project) or "", "project": project}

        if mode in ("store", "update"):
            if content:
                store.upsert_summary(project, content)
            elif sections:
                existing = store.get_summary(project) or ""
                for section_name, section_text in sections.items():
                    header = f"## {section_name}"
                    if header in existing:
                        existing = re.sub(
                            rf"(## {re.escape(section_name)})(.*?)(?=\n## |\Z)",
                            f"\\1\n{section_text}",
                            existing,
                            flags=re.DOTALL,
                        )
                    else:
                        existing += f"\n\n{header}\n{section_text}"
                store.upsert_summary(project, existing.strip())
            store.commit()
            return {"status": "stored", "project": project}

        if mode == "delete":
            store.upsert_summary(project, "")
            store.commit()
            return {"status": "deleted", "project": project}

        return {"error": f"Unknown ADR mode: {mode}"}

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
