"""
SnippetService — retrieve and format code content from the graph and disk.

Covers:
  - get_code_snippet  (symbol body, neighbors, or enclosing class)
  - trace_call_path   (BFS over CALLS edges with optional risk labels)
  - detect_changes    (git diff → changed symbols → impacted callers)
  - _find_enclosing_class / _read_lines  (internal helpers)
  - _git_changed_files
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections import deque
from pathlib import Path

from backend.graph.store import GraphStore

log = logging.getLogger(__name__)

_RISK = {0: "CRITICAL", 1: "HIGH", 2: "MEDIUM", 3: "LOW"}


class SnippetService:
    """All methods are stateless given the store, project, and root path."""

    # ------------------------------------------------------------------
    # get_code_snippet
    # ------------------------------------------------------------------

    def get_code_snippet(
        self,
        store: GraphStore,
        project: str,
        qualified_name: str,
        auto_resolve: bool = True,
        include_neighbors: bool = False,
        scope: str = "procedure",
    ) -> dict:
        """
        Fetch source for a symbol.

        scope:
          'procedure' — the symbol's own body (default).
          'neighbors' — 20 lines above and below the symbol.
          'class'     — the enclosing class body (falls back to procedure if
                        the symbol is not inside a class).
        """
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

    # ------------------------------------------------------------------
    # trace_call_path
    # ------------------------------------------------------------------

    def trace_call_path(
        self,
        store: GraphStore,
        project: str,
        function_name: str,
        direction: str = "both",
        depth: int = 3,
        risk_labels: bool = False,
        min_confidence: float = 0.0,
    ) -> dict:
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
                        confidence = json.loads(edge.get("properties") or "{}").get(
                            "confidence", 1.0
                        )
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

    def detect_changes(
        self,
        store: GraphStore,
        project: str,
        root: str,
        scope: str = "all",
        depth: int = 3,
    ) -> dict:
        """
        Detect which graph symbols are in files modified according to git,
        then BFS-expand to find all impacted callers.
        """
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
                store,
                project,
                symbol["name"],
                direction="inbound",
                depth=depth,
                risk_labels=True,
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_enclosing_class(
        self, store: GraphStore, project: str, node: dict
    ) -> dict | None:
        """Return the tightest Class node that contains *node* in the same file."""
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

    def _git_changed_files(self, root: str, scope: str) -> list[str]:
        try:
            if scope == "staged":
                cmd = ["git", "diff", "--cached", "--name-only"]
            elif scope == "unstaged":
                cmd = ["git", "diff", "--name-only"]
            else:
                cmd = ["git", "diff", "HEAD", "--name-only"]
            output = subprocess.check_output(
                cmd, cwd=root, text=True, stderr=subprocess.DEVNULL
            )
            return [
                str(Path(root) / file_name)
                for file_name in output.strip().splitlines()
                if file_name
            ]
        except Exception:
            return []
