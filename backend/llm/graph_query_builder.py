"""
GraphQueryBuilder — targeted graph API calls for context assembly.

Wraps GraphClient with context-specific query patterns and fallback handling.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.graph.client import GraphClient
from backend.llm.context_formatter import read_file
from backend.session.models import Task

log = logging.getLogger(__name__)


class GraphQueryBuilder:
    """Issues targeted codebase-memory queries for LLM context assembly."""

    def __init__(self, graph: GraphClient):
        self._g = graph

    def get_architecture(self) -> tuple[str, str]:
        """
        Returns (arch_text, adr_text). Both are empty strings on failure.
        adr_text already includes its section header.
        """
        from backend.llm.context_formatter import format_architecture, format_adr
        try:
            arch = self._g.get_architecture(
                ["entry_points", "hotspots", "clusters", "layers", "adr"]
            )
            return format_architecture(arch), format_adr(arch.get("adr"))
        except Exception as e:
            log.warning("get_architecture failed: %s", e)
            return "(graph not available)", ""

    def count_open_todos(self) -> int:
        """Count TODO/FIXME/HACK markers in the codebase."""
        try:
            todos = self._g.search_code("TODO|FIXME|HACK", regex=True)
            return todos.get("total", 0)
        except Exception as e:
            log.debug("TODO search failed: %s", e)
            return 0

    def get_target_code(self, task: Task) -> str:
        """
        Retrieve the exact body of the target function via get_code_snippet.
        Falls back to reading the raw file (capped at 3000 chars) if the graph
        cannot find it.
        """
        if not task.target_function:
            if task.target_file:
                return (read_file(task.target_file) or "")[:3000]
            return "(no target specified)"

        try:
            qname = self._g.find_qualified_name(task.target_function)
            if qname:
                result = self._g.get_code_snippet(qname, auto_resolve=True)
                source = result.get("source") or result.get("snippet", "")
                if source:
                    sig = result.get("signature", "")
                    header = f"// {sig}\n" if sig else ""
                    return header + source
        except Exception as e:
            log.debug("get_target_code graph lookup failed: %s", e)

        if task.target_file:
            return (read_file(task.target_file) or "")[:3000]
        return "(could not retrieve code)"

    def get_outbound_calls(self, task: Task) -> str:
        """What does the target function call? (outbound, depth 2)"""
        if not task.target_function:
            return "(none)"
        try:
            result = self._g.trace_call_path(
                task.target_function, direction="outbound", depth=2
            )
            nodes = result.get("nodes", [])
            deps = [n for n in nodes if n.get("name") != task.target_function]
            if not deps:
                return "(none)"
            return "\n".join(
                f"  • {n.get('name')} — {n.get('file_path', '?')}:{n.get('start_line', '?')}"
                for n in deps[:15]
            )
        except Exception as e:
            log.debug("get_outbound_calls failed: %s", e)
            return "(graph query failed)"

    def get_inbound_with_risk(self, task: Task) -> tuple[str, str]:
        """
        Who calls the target function? Returns (callers_text, risk_summary).
        """
        if not task.target_function:
            return "(none)", ""
        try:
            result = self._g.trace_call_path(
                task.target_function,
                direction="inbound",
                depth=3,
                risk_labels=True,
            )
            nodes = result.get("nodes", [])
            callers = [n for n in nodes if n.get("name") != task.target_function]
            if not callers:
                return "(none)", ""

            lines = []
            for n in callers[:15]:
                risk = n.get("risk", "")
                tag = f"[{risk}] " if risk else ""
                lines.append(
                    f"  • {tag}{n.get('name')} "
                    f"— {n.get('file_path', '?')}:{n.get('start_line', '?')}"
                )

            impact = result.get("impact_summary", "")
            risk_summary = f"\nIMPACT SUMMARY: {impact}\n" if impact else ""
            return "\n".join(lines), risk_summary
        except Exception as e:
            log.debug("get_inbound_with_risk failed: %s", e)
            return "(graph query failed)", ""

    def get_pending_changes(self) -> str:
        """List files already modified in the working tree."""
        try:
            result = self._g.detect_changes(scope="unstaged", depth=1)
            files = result.get("changed_files", [])
            if not files:
                return "(clean working tree)"
            return "\n".join(f"  {f}" for f in files[:10])
        except Exception:
            return "(git unavailable)"

    def get_scan_context(self, task: Task) -> str:
        """
        Maps uncommitted changes to the blast radius via detect_changes.
        Feeds RIPPLE_DETECTION prompt.
        """
        try:
            result = self._g.detect_changes(scope="unstaged", depth=3)
        except Exception as e:
            log.warning("get_scan_context: detect_changes failed: %s", e)
            return "SCAN: git diff unavailable — inspect callers manually."

        changed = result.get("changed_symbols", [])
        impacted = result.get("impacted_callers", [])
        changed_files = result.get("changed_files", [])

        if not changed and not changed_files:
            return "SCAN: No uncommitted changes detected."

        changed_str = "\n".join(
            f"  [{s.get('risk', '?')}] {s.get('qualified_name') or s.get('name', '?')}"
            for s in changed
        ) or "  (symbols not resolved)"

        impacted_str = "\n".join(
            f"  [{s.get('risk', '?')}] {s.get('qualified_name') or s.get('name', '?')}"
            "  — " + s.get("file_path", "")
            for s in impacted[:20]
        ) or "  (none)"

        files_str = "\n".join(f"  {f}" for f in changed_files[:15])

        return (
            f"SCAN: Post-execution ripple analysis for '{task.title}'\n\n"
            f"CHANGED FILES:\n{files_str}\n\n"
            f"CHANGED SYMBOLS:\n{changed_str}\n\n"
            f"IMPACTED CALLERS (review before committing):\n{impacted_str}"
        )
