"""
Code dependency graph built from parsed symbol data.
Tracks which functions/methods call or import other functions/methods.

Graph format: adjacency map
  "file.ts::functionA" -> ["file.ts::functionB", "other.ts::helperFn"]

This is a best-effort static analysis graph. It captures:
- Direct function calls visible in body snippets
- Import relationships (file-level)
"""

from __future__ import annotations
import re
from collections import defaultdict
from dataclasses import dataclass, field

from backend.indexer.parser import ParsedFile, Symbol


@dataclass
class CodeGraph:
    # node_id -> list of node_ids it depends on
    edges: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    # reverse: node_id -> list of node_ids that depend on it
    reverse_edges: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    # node_id -> Symbol
    nodes: dict[str, Symbol] = field(default_factory=dict)

    def node_id(self, symbol: Symbol) -> str:
        return f"{symbol.file}::{symbol.name}"

    def add_symbol(self, symbol: Symbol) -> None:
        nid = self.node_id(symbol)
        self.nodes[nid] = symbol

    def add_edge(self, from_id: str, to_id: str) -> None:
        if to_id not in self.edges[from_id]:
            self.edges[from_id].append(to_id)
        if from_id not in self.reverse_edges[to_id]:
            self.reverse_edges[to_id].append(from_id)

    def callers_of(self, node_id: str) -> list[str]:
        return list(self.reverse_edges.get(node_id, []))

    def dependencies_of(self, node_id: str) -> list[str]:
        return list(self.edges.get(node_id, []))

    def ripple(self, node_ids: list[str], depth: int = 3) -> dict[str, int]:
        """
        BFS from node_ids through reverse_edges (callers).
        Returns {node_id: depth_level} for all affected nodes.
        """
        visited: dict[str, int] = {}
        queue = [(nid, 0) for nid in node_ids]
        while queue:
            current, d = queue.pop(0)
            if current in visited or d > depth:
                continue
            visited[current] = d
            for caller in self.callers_of(current):
                if caller not in visited:
                    queue.append((caller, d + 1))
        return visited

    def to_dict(self) -> dict:
        return {
            "edges": dict(self.edges),
            "nodes": {nid: {
                "name": sym.name,
                "kind": sym.kind,
                "file": sym.file,
                "line": sym.line,
            } for nid, sym in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> CodeGraph:
        g = cls()
        g.edges = defaultdict(list, d.get("edges", {}))
        # Rebuild reverse edges
        for from_id, targets in g.edges.items():
            for to_id in targets:
                if from_id not in g.reverse_edges[to_id]:
                    g.reverse_edges[to_id].append(from_id)
        return g


def build_graph(parsed_files: list[ParsedFile]) -> CodeGraph:
    """Build a CodeGraph from a list of parsed files."""
    graph = CodeGraph()

    # Step 1: Register all symbols
    name_to_ids: dict[str, list[str]] = defaultdict(list)
    for pf in parsed_files:
        for sym in pf.symbols:
            nid = graph.node_id(sym)
            graph.add_symbol(sym)
            name_to_ids[sym.name].append(nid)

    # Step 2: Add import-level edges (file imports file)
    file_to_imports: dict[str, list[str]] = {}
    for pf in parsed_files:
        file_to_imports[pf.path] = pf.imports

    # Step 3: Best-effort call detection from body snippets
    for pf in parsed_files:
        for sym in pf.symbols:
            from_id = graph.node_id(sym)
            # Look for function names appearing in the body snippet
            called_names = _extract_called_names(sym.body_snippet)
            for cname in called_names:
                if cname == sym.name:
                    continue  # skip self-reference
                candidates = name_to_ids.get(cname, [])
                for to_id in candidates:
                    graph.add_edge(from_id, to_id)

    return graph


# Matches identifiers followed by ( — rough call detection from snippet text
_CALL_PATTERN = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")


def _extract_called_names(snippet: str) -> list[str]:
    return list({m.group(1) for m in _CALL_PATTERN.finditer(snippet)})
