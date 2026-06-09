"""
IndexerOrchestrator — drives the full index_repository pipeline.

Responsible for:
  - Running graphify's extraction pipeline (40+ languages) when available,
    falling back to WaterFree's built-in IndexPipeline (5 languages).
  - Writing/updating nodes, edges, and file hashes into GraphStore.
  - Cleaning up deleted files from the store.
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.graph.store import GraphStore

log = logging.getLogger(__name__)


class IndexerOrchestrator:
    """Stateless orchestrator; receives an already-open store and project name."""

    def index_repository(
        self,
        store: GraphStore,
        project: str,
        root: str,
        indexing_set: set[str],
    ) -> dict:
        indexing_set.add(project)
        try:
            # Remove stale project entries that share the same root path but a
            # different slug (happens after rename/move).
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

            # Try the graphify pipeline first; fall back to built-in if unavailable
            try:
                from backend.graph.graphify_adapter import is_available as _gfy_ok
                if _gfy_ok():
                    return self._index_with_graphify(store, project, root)
            except Exception as _e:
                log.warning("graphify indexing failed, falling back to built-in: %s", _e)

            return self._index_with_builtin(store, project, root)
        finally:
            indexing_set.discard(project)

    # ------------------------------------------------------------------
    # graphify pipeline
    # ------------------------------------------------------------------

    def _index_with_graphify(self, store: GraphStore, project: str, root: str) -> dict:
        from backend.graph.graphify_adapter import (
            run_extraction,
            to_store_nodes,
            to_store_edges,
        )

        log.info("Indexing %r with graphify (40+ languages)", project)
        nodes_raw, edges_raw = run_extraction(root)

        if not nodes_raw:
            return {
                "project": project,
                "files_indexed": 0,
                "deleted_files": 0,
                "nodes": 0,
                "edges": 0,
                "status": "up_to_date",
                "engine": "graphify",
            }

        store_nodes = to_store_nodes(nodes_raw, project, root)
        id_to_qn = {n["graphify_id"]: n["qualified_name"] for n in store_nodes if n.get("graphify_id")}
        store_edges = to_store_edges(edges_raw, id_to_qn)

        # Wipe existing data for this project and re-insert from scratch.
        # graphify rebuilds the whole graph on each run — incremental caching is
        # handled internally by graphify's own cache layer.
        store.delete_project(project)
        store.upsert_project(project, root)

        for n in store_nodes:
            store.upsert_node(
                project,
                n["label"],
                n["name"],
                n["qualified_name"],
                n["file_path"],
                n["line"],
                n["end_line"],
                n["metadata"],
            )
        store.commit()

        # Build QN→id map for edge insertion
        qn_to_id: dict[str, int] = {}
        for node in store.get_all_nodes(project):
            qn_to_id[node["qualified_name"]] = node["id"]

        for e in store_edges:
            src_id = qn_to_id.get(e["source_qn"])
            tgt_id = qn_to_id.get(e["target_qn"])
            if src_id and tgt_id:
                store.upsert_edge(project, src_id, tgt_id, e["relation"], e["metadata"])
        store.commit()

        # Persist file hashes so incremental re-index knows what's current
        files_seen: set[str] = set()
        for n in store_nodes:
            fp = n.get("file_path") or ""
            if fp and fp not in files_seen:
                files_seen.add(fp)
                try:
                    rel = str(Path(fp).relative_to(root)).replace("\\", "/")
                except ValueError:
                    rel = fp
                from backend.graph.indexer import file_hash
                sha = file_hash(fp)
                if sha:
                    store.upsert_file_hash(project, rel, sha)
        store.commit()

        total_nodes = len(store.get_all_nodes(project))
        total_edges = store.count_edges(project)

        return {
            "project": project,
            "files_indexed": len(files_seen),
            "deleted_files": 0,
            "nodes": total_nodes,
            "edges": total_edges,
            "status": "indexed",
            "engine": "graphify",
        }

    # ------------------------------------------------------------------
    # Built-in pipeline (fallback)
    # ------------------------------------------------------------------

    def _index_with_builtin(self, store: GraphStore, project: str, root: str) -> dict:
        from backend.graph.indexer import IndexPipeline, resolve_calls

        log.info("Indexing %r with built-in indexer (5 languages)", project)

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
                "engine": "builtin",
            }

        name_to_qns: dict[str, list[str]] = {}
        for node in store.get_all_nodes(project):
            name_to_qns.setdefault(node["name"], []).append(node["qualified_name"])

        # Phase 1: upsert nodes
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

        # Phase 2: upsert edges
        for parsed_file in parsed:
            for src_qn, tgt_qn in resolve_calls(parsed_file, name_to_qns):
                source = store.get_node_by_qn(project, src_qn)
                target = store.get_node_by_qn(project, tgt_qn)
                if source and target:
                    store.upsert_edge(project, source["id"], target["id"], "CALLS", {"confidence": 0.6})

            mod_node = store.get_node_by_qn(project, parsed_file.module_qn_prefix)
            if mod_node:
                for sym in parsed_file.symbols:
                    sym_node = store.get_node_by_qn(project, sym.qualified_name)
                    if sym_node:
                        store.upsert_edge(project, mod_node["id"], sym_node["id"], "DEFINES")

            for sym in parsed_file.symbols:
                if sym.label != "Class":
                    continue
                sym_node = store.get_node_by_qn(project, sym.qualified_name)
                if not sym_node:
                    continue
                for base in sym.properties.get("base_classes", []):
                    for base_node in store.find_nodes_by_name(project, base, "Class")[:1]:
                        store.upsert_edge(project, sym_node["id"], base_node["id"], "INHERITS")

        store.commit()

        # Phase 3: persist hashes
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
            "engine": "builtin",
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
