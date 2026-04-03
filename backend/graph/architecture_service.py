"""
ArchitectureService — high-level architecture analysis queries.

Covers:
  - get_architecture (languages, entry points, hotspots, layers, clusters, ADR)
  - manage_adr (store / retrieve / update architecture decision records)
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from pathlib import Path

from backend.graph.store import GraphStore


class ArchitectureService:
    """Stateless service; receives an already-open store and project/root strings."""

    def get_architecture(
        self,
        store: GraphStore,
        project: str,
        root: str,
        aspects: list[str] | None = None,
    ) -> dict:
        aspects = aspects or ["all"]
        out: dict = {}

        if "all" in aspects or "languages" in aspects:
            out["languages"] = self._languages(store, project)
        if "all" in aspects or "entry_points" in aspects:
            out["entry_points"] = self._entry_points(store, project)
        if "all" in aspects or "hotspots" in aspects:
            out["hotspots"] = self._hotspots(store, project)
        if "all" in aspects or "layers" in aspects:
            out["layers"] = self._layers(store, project, root)
        if "all" in aspects or "clusters" in aspects:
            out["clusters"] = self._clusters(store, project)
        if "all" in aspects or "module_graph" in aspects:
            out["module_graph"] = self._module_graph(store, project, root)
        if "all" in aspects or "adr" in aspects:
            text = store.get_summary(project)
            if text:
                out["adr"] = {"text": text}

        return out

    def manage_adr(
        self,
        store: GraphStore,
        project: str,
        mode: str,
        content: str = "",
        sections: dict | None = None,
    ) -> dict:
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

    # ── Internal analysis helpers ─────────────────────────────────────────────

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

    def _layers(self, store: GraphStore, project: str, root: str) -> list[dict]:
        layer_map: dict[str, list[str]] = {}
        for module in store.get_all_nodes(project, "Module"):
            file_path = module["file_path"] or ""
            rel = self._relative_path(file_path, root)
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
                queue.extend(
                    neighbor
                    for neighbor in adjacency.get(current, set())
                    if neighbor not in visited
                )
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

    def _module_graph(self, store: GraphStore, project: str, root: str) -> dict:
        node_stats: dict[str, dict] = {}
        for node in store.get_all_nodes(project):
            file_path = node.get("file_path") or ""
            if not file_path:
                continue
            rel_path = self._relative_path(file_path, root)
            group = Path(rel_path).parts[0] if len(Path(rel_path).parts) > 1 else "(root)"
            stats = node_stats.setdefault(
                file_path,
                {
                    "id": rel_path,
                    "path": rel_path,
                    "name": Path(rel_path).name,
                    "group": group,
                    "symbol_count": 0,
                    "call_in": 0,
                    "call_out": 0,
                    "entry_point": False,
                    "score": 0.0,
                },
            )

            label = str(node.get("label", ""))
            if label != "Module":
                stats["symbol_count"] += 1
            if str(node.get("name", "")).lower() in {"main", "index", "app", "server", "extension", "__main__"}:
                stats["entry_point"] = True

        link_counts: dict[tuple[str, str], dict] = {}
        for edge in store.get_all_edges(project, "CALLS"):
            source_file = edge.get("source_file_path") or ""
            target_file = edge.get("target_file_path") or ""
            if not source_file or not target_file or source_file == target_file:
                continue

            source_stats = node_stats.get(source_file)
            target_stats = node_stats.get(target_file)
            if source_stats:
                source_stats["call_out"] += 1
            if target_stats:
                target_stats["call_in"] += 1

            source_id = self._relative_path(source_file, root)
            target_id = self._relative_path(target_file, root)
            link = link_counts.setdefault(
                (source_id, target_id),
                {
                    "source": source_id,
                    "target": target_id,
                    "weight": 0,
                },
            )
            link["weight"] += 1

        ranked_modules = []
        for stats in node_stats.values():
            stats["score"] = (
                float(stats["symbol_count"])
                + float(stats["call_in"]) * 2.0
                + float(stats["call_out"])
                + (8.0 if stats["entry_point"] else 0.0)
            )
            ranked_modules.append(stats)
        ranked_modules.sort(key=lambda item: (-item["score"], item["path"]))

        max_nodes = 48
        selected = ranked_modules[:max_nodes]
        selected_ids = {item["id"] for item in selected}
        groups = sorted({item["group"] for item in selected})
        group_index = {name: index for index, name in enumerate(groups)}

        graph_nodes = [
            {
                "id": item["id"],
                "path": item["path"],
                "label": item["name"],
                "group": item["group"],
                "group_index": group_index[item["group"]],
                "symbol_count": item["symbol_count"],
                "call_in": item["call_in"],
                "call_out": item["call_out"],
                "entry_point": item["entry_point"],
                "radius": round(8 + math.sqrt(max(item["symbol_count"], 1)) * 3, 2),
            }
            for item in selected
        ]

        graph_links = [
            link
            for link in sorted(link_counts.values(), key=lambda item: (-item["weight"], item["source"], item["target"]))
            if link["source"] in selected_ids and link["target"] in selected_ids
        ][:160]

        return {
            "nodes": graph_nodes,
            "links": graph_links,
            "groups": groups,
            "total_modules": len(node_stats),
            "visible_modules": len(graph_nodes),
        }

    def _relative_path(self, file_path: str, root: str) -> str:
        if not file_path:
            return ""
        try:
            return str(Path(file_path).relative_to(root)).replace("\\", "/")
        except ValueError:
            return file_path.replace("\\", "/")
