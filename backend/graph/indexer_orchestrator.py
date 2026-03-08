"""
IndexerOrchestrator — drives the full index_repository pipeline.

Responsible for:
  - Running IndexPipeline against a workspace root
  - Writing/updating nodes, edges, and file hashes into GraphStore
  - Cleaning up deleted files from the store
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.graph.indexer import IndexPipeline, resolve_calls
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
        """
        Index *root* into *store* under *project*.

        *indexing_set* is the engine's ``_indexing`` set; the orchestrator
        adds/removes the project around the run so callers can detect an
        in-progress index via ``index_status``.
        """
        indexing_set.add(project)
        try:
            # Remove any legacy project entries that share the same root path
            # but have a different slug (happens after a rename/move).
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

            # Build a name→[qualified_name] map from *existing* graph nodes so
            # that call-resolution can match short names to their full QNs.
            name_to_qns: dict[str, list[str]] = {}
            for node in store.get_all_nodes(project):
                name_to_qns.setdefault(node["name"], []).append(node["qualified_name"])

            # --- Phase 1: upsert nodes for every changed file ---
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

            # --- Phase 2: upsert edges (CALLS, DEFINES, INHERITS) ---
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

            # --- Phase 3: persist updated file hashes ---
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
            indexing_set.discard(project)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup_deleted_files(
        self,
        store: GraphStore,
        project: str,
        root: str,
        stored_hashes: dict[str, str],
        current_rels: list[str],
    ) -> int:
        """Remove graph data for files that no longer exist on disk."""
        deleted = sorted(set(stored_hashes) - set(current_rels))
        for rel_path in deleted:
            abs_path = str(Path(root) / rel_path)
            store.delete_edges_for_file(project, abs_path)
            store.delete_nodes_for_file(project, abs_path)
            store.delete_file_hash(project, rel_path)
        if deleted:
            store.commit()
        return len(deleted)
