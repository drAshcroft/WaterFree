"""
QueryService — answers "find me this" questions against an indexed graph.

Covers:
  - search_graph  (structured node/edge filtering)
  - search_code   (regex/literal full-text search over raw source files)
  - find_qualified_name (short-name → qualified-name lookup)
  - query_graph   (lightweight pseudo-Cypher interpreter)
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from backend.graph.indexer import collect_files
from backend.graph.store import GraphStore


def _compile_pattern(pattern: str, case_sensitive: bool) -> re.Pattern[str] | None:
    if not pattern:
        return None
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error:
        return re.compile(re.escape(pattern), flags)


class QueryService:
    """All methods are pure functions of (store, project, args) — no mutable state."""

    # ------------------------------------------------------------------
    # search_graph
    # ------------------------------------------------------------------

    def search_graph(
        self,
        store: GraphStore,
        project: str,
        root: str,
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
        **_kwargs,
    ) -> dict:
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
                if not fnmatch.fnmatch(rel_path, file_pattern) and not fnmatch.fnmatch(
                    file_path, file_pattern
                ):
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
        page = results[offset : offset + limit]
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
        root: str,
        pattern: str,
        regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 50,
        offset: int = 0,
        file_pattern: str = "",
    ) -> dict:
        flags = re.MULTILINE | (0 if case_sensitive else re.IGNORECASE)
        try:
            search_re = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as exc:
            return {"matches": [], "total": 0, "error": str(exc), "has_more": False}

        root_path = root or "."
        all_matches: list[dict] = []
        for file_path in collect_files(root_path):
            rel_path = (
                str(file_path.relative_to(root_path))
                if Path(root_path) in file_path.parents
                else str(file_path)
            )
            if file_pattern and not fnmatch.fnmatch(rel_path, file_pattern) and not fnmatch.fnmatch(
                str(file_path), file_pattern
            ):
                continue

            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            lines = text.splitlines()
            for match in search_re.finditer(text):
                line_no = text[: match.start()].count("\n") + 1
                all_matches.append(
                    {
                        "file": str(file_path),
                        "line": line_no,
                        "text": lines[line_no - 1].strip() if line_no <= len(lines) else "",
                        "match": match.group(0),
                    }
                )

        total = len(all_matches)
        page = all_matches[offset : offset + max_results]
        return {
            "matches": page,
            "total": total,
            "has_more": offset + max_results < total,
            "limit": max_results,
            "offset": offset,
        }

    # ------------------------------------------------------------------
    # find_qualified_name
    # ------------------------------------------------------------------

    def find_qualified_name(self, store: GraphStore, project: str, short_name: str) -> str | None:
        candidates = store.find_nodes_by_name(project, short_name)
        if candidates:
            return candidates[0]["qualified_name"]
        nodes = store.search_nodes(project, short_name, limit=5)
        return nodes[0]["qualified_name"] if nodes else None

    # ------------------------------------------------------------------
    # query_graph
    # ------------------------------------------------------------------

    def query_graph(self, store: GraphStore, project: str, query: str) -> dict:
        """
        Lightweight pseudo-Cypher interpreter.

        Supports a small subset of patterns:
          - ``(n:Label)``                         — filter by label
          - ``name =~ 'regex'`` / ``name = 'x'`` — filter by name
          - ``CALLS ->``                          — expand outbound CALLS edges
        """
        label_match = re.search(r"\([\w]+:([\w]+)\)", query)
        label = label_match.group(1) if label_match else ""
        name_regex = re.search(r"name\s*=~\s*['\"]([^'\"]+)['\"]", query)
        name_equals = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", query)

        if name_regex:
            nodes = store.search_nodes(
                project, name_regex.group(1).lstrip("(?i)"), label=label, limit=50
            )
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
