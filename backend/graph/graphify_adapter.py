"""
Adapter between graphify's extraction pipeline and WaterFree's graph store.

graphify handles 40+ languages via tree-sitter; this adapter runs its
extract→build→analyze pipeline and converts results into the format that
IndexerOrchestrator and ArchitectureService expect.

Falls back gracefully when graphify or its optional deps are missing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# graphify's output (cache/ast, stat-index, graph.json, …) lives under the
# workspace-local .waterfree/ folder — that default is set in the graphify
# package itself (backend.graph.graphify.cache._GRAPHIFY_OUT), resolved against
# the indexed project root, so no env-var juggling is needed here.

# ---------------------------------------------------------------------------
# Optional graphify import
# ---------------------------------------------------------------------------

try:
    from backend.graph.graphify.extract import collect_files as _gfy_collect_files
    from backend.graph.graphify.extract import extract as _gfy_extract
    from backend.graph.graphify.build import build_from_json as _gfy_build
    from backend.graph.graphify.analyze import god_nodes as _gfy_god_nodes
    from backend.graph.graphify.analyze import surprising_connections as _gfy_surprising
    from backend.graph.graphify.analyze import find_import_cycles as _gfy_import_cycles
    import networkx as _nx
    _GFY_OK = True
except Exception as _e:
    _GFY_OK = False
    log.warning("graphify not available — falling back to built-in indexer (%s)", _e)


def is_available() -> bool:
    return _GFY_OK


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def run_extraction(root_path: str) -> tuple[list[dict], list[dict]]:
    """
    Run graphify's extract+build pipeline on *root_path*.

    Returns (nodes, edges) as flat lists of attribute dicts:
      node: {"id", "label", "source_file", "source_location", ...}
      edge: {"source", "target", "relation", "source_file", ...}
    """
    if not _GFY_OK:
        raise RuntimeError("graphify not available")

    root = Path(root_path).resolve()
    files = _gfy_collect_files(root)
    if not files:
        return [], []

    extraction = _gfy_extract(files, cache_root=root)
    G = _gfy_build(extraction)

    nodes = [{"id": nid, **attrs} for nid, attrs in G.nodes(data=True)]
    edges = [{"source": src, "target": tgt, **attrs} for src, tgt, attrs in G.edges(data=True)]
    return nodes, edges


def _build_nx_graph(nodes: list[dict], edges: list[dict]) -> "_nx.DiGraph":
    """Assemble a networkx DiGraph from WaterFree node/edge dicts for analysis."""
    G = _nx.DiGraph()
    for node in nodes:
        nid = node.get("id") or node.get("qualified_name") or ""
        if nid:
            G.add_node(nid, **{k: v for k, v in node.items() if k != "id"})
    for edge in edges:
        src = edge.get("source") or edge.get("graphify_source_id") or ""
        tgt = edge.get("target") or edge.get("graphify_target_id") or ""
        if src and tgt and G.has_node(src) and G.has_node(tgt):
            G.add_edge(src, tgt, **{k: v for k, v in edge.items() if k not in ("source", "target")})
    return G


def run_analysis(nodes: list[dict], edges: list[dict]) -> dict:
    """
    Run graphify's analysis (god nodes, surprising connections) on a node/edge list.
    Returns a dict with "god_nodes" and "surprising_connections" keys.
    """
    if not _GFY_OK:
        return {"god_nodes": [], "surprising_connections": []}

    G = _build_nx_graph(nodes, edges)
    if not G.number_of_nodes():
        return {"god_nodes": [], "surprising_connections": []}

    return {
        "god_nodes": _gfy_god_nodes(G, top_n=12),
        "surprising_connections": _gfy_surprising(G, top_n=8),
    }


def run_import_cycles(
    nodes: list[dict],
    edges: list[dict],
    max_cycle_length: int = 5,
    top_n: int = 20,
) -> list[dict]:
    """
    Detect circular import dependencies (file-level) via graphify's analyzer.

    Edges must carry relation "imports_from" (the relation graphify's cycle
    detector keys on) and a "source_file" attribute identifying the importing
    file; nodes must carry "source_file". The caller is responsible for that
    mapping — see ArchitectureService._import_cycles.

    Returns a list of {"cycle": [...files...], "length": int, "why": str}.
    """
    if not _GFY_OK:
        return []
    G = _build_nx_graph(nodes, edges)
    if not G.number_of_nodes():
        return []
    return _gfy_import_cycles(G, max_cycle_length=max_cycle_length, top_n=top_n)


# ---------------------------------------------------------------------------
# Format conversion: graphify nodes/edges → WaterFree store format
# ---------------------------------------------------------------------------

_LABEL_MAP: dict[str, str] = {
    "function": "Function",
    "method": "Method",
    "class": "Class",
    "interface": "Class",
    "struct": "Class",
    "enum": "Class",
    "trait": "Class",
    "type": "Class",
    "file": "Module",
    "module": "Module",
}


def _parse_line(loc: str | None) -> int:
    """Parse 'L42' or '42' source_location into an int."""
    if not loc:
        return 1
    try:
        return int(str(loc).lstrip("L").split(":")[0])
    except (ValueError, AttributeError):
        return 1


def _clean_symbol_name(raw: object) -> str:
    """Normalize graphify display labels into WaterFree symbol names."""
    name = str(raw or "").strip()
    return re.sub(r"\(\)$", "", name)


def to_store_nodes(
    nodes: list[dict],
    project: str,
    root_path: str,
) -> list[dict]:
    """
    Convert graphify node dicts to the format expected by GraphStore.upsert_node().

    Each returned dict has keys:
      label, name, qualified_name, file_path, line, end_line, metadata
    and a bonus "graphify_id" key used by to_store_edges() to build edge maps.
    """
    root = Path(root_path).resolve()
    result: list[dict] = []

    for node in nodes:
        raw_source_file = node.get("source_file") or ""
        if not raw_source_file:
            continue
        source_path = Path(str(raw_source_file))
        if not source_path.is_absolute():
            source_path = root / source_path
        source_file = str(source_path.resolve())

        raw_label = str(node.get("label") or "")
        node_id = str(node.get("id") or "")

        # graphify uses the actual filename as the label for file-level nodes
        is_file_node = raw_label == Path(source_file).name
        if is_file_node:
            label = "Module"
        else:
            label = _LABEL_MAP.get(raw_label.lower(), "Function")

        name = _clean_symbol_name(node.get("name") or raw_label or node_id or "unknown")

        # For Module nodes the name is the clean file stem
        if label == "Module":
            name = Path(source_file).stem

        line = _parse_line(node.get("source_location"))

        try:
            rel = str(Path(source_file).relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = str(Path(source_file)).replace("\\", "/")

        mod_part = str(Path(rel).with_suffix("")).replace("\\", "/").replace("/", ".").lstrip(".")
        if label == "Module":
            qualified_name = f"{project}.{mod_part}"
        else:
            qualified_name = f"{project}.{mod_part}.{name}"

        result.append({
            "label": label,
            "name": name,
            "qualified_name": qualified_name,
            "file_path": source_file,
            "line": line,
            "end_line": line,
            "graphify_id": node_id,
            "metadata": {
                "graphify_id": node_id,
                "file_type": node.get("file_type", "code"),
                "body_snippet": str(node.get("body") or "")[:200],
                "language": Path(source_file).suffix.lstrip(".") or "unknown",
            },
        })

    return result


def to_store_edges(
    edges: list[dict],
    id_to_qn: dict[str, str],
) -> list[dict]:
    """
    Convert graphify edge dicts to WaterFree store format using *id_to_qn*
    (a mapping from graphify node IDs → qualified_name strings).

    Each returned dict has keys: source_qn, target_qn, relation, metadata.
    """
    _RELATION_MAP: dict[str, str] = {
        "calls": "CALLS",
        "imports": "IMPORTS",
        "inherits": "INHERITS",
        "implements": "INHERITS",
        "contains": "DEFINES",
        "method": "DEFINES",
        "imports_from": "IMPORTS",
        "re_exports": "IMPORTS",
    }

    result: list[dict] = []
    for edge in edges:
        src_id = str(edge.get("_src") or edge.get("source") or "")
        tgt_id = str(edge.get("_tgt") or edge.get("target") or "")
        src_qn = id_to_qn.get(src_id)
        tgt_qn = id_to_qn.get(tgt_id)
        if not src_qn or not tgt_qn or src_qn == tgt_qn:
            continue

        raw_rel = str(edge.get("relation") or "calls").lower()
        relation = _RELATION_MAP.get(raw_rel, "CALLS")

        result.append({
            "source_qn": src_qn,
            "target_qn": tgt_qn,
            "relation": relation,
            "metadata": {
                "confidence": edge.get("confidence", "EXTRACTED"),
                "weight": float(edge.get("weight") or 1.0),
            },
        })

    return result
