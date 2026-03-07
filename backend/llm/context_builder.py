"""
ContextBuilder — assembles per-turn LLM context strings via graph queries.

Every context method issues targeted codebase-memory-mcp queries rather than
reading raw files or walking an adjacency map. This gives Claude:

  Planning   → architecture overview (layers, clusters, hotspots, ADR) in ~500 tokens
  Annotation → exact function source + import-aware call chains + risk-labelled blast radius
               + uncommitted changes already in the working tree
  Execution  → approved intent + full contents of every file to be touched
  Scan       → post-execution git diff mapped to callers with CRITICAL/HIGH/MEDIUM/LOW risk
  Question   → minimal (goal + current task)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from backend.graph.client import GraphClient
from backend.knowledge import retriever as knowledge_retriever
from backend.llm.context_lifecycle import ContextLifecycleManager
from backend.session.models import AnnotationStatus, IntentAnnotation, PlanDocument, Task

log = logging.getLogger(__name__)


class ContextBuilder:
    def __init__(self, graph: GraphClient):
        self._g = graph
        self._lifecycle = ContextLifecycleManager()

    # ------------------------------------------------------------------
    # Planning context — architecture overview before any code is touched
    # ------------------------------------------------------------------

    def build_planning_context(self, goal: str, plan: PlanDocument) -> str:
        arch_text = "(graph not available)"
        adr_text = ""
        todo_count = 0

        try:
            arch = self._g.get_architecture(
                ["entry_points", "hotspots", "clusters", "layers", "adr"]
            )
            arch_text = _format_architecture(arch)
            adr_text = _format_adr(arch.get("adr"))
        except Exception as e:
            log.warning("build_planning_context: get_architecture failed: %s", e)

        try:
            todos = self._g.search_code("TODO|FIXME|HACK", regex=True)
            todo_count = todos.get("total", 0)
        except Exception as e:
            log.debug("build_planning_context: TODO search failed: %s", e)

        completed = plan.completed_tasks()
        completed_str = ", ".join(t.title for t in completed) or "none yet"

        memory_text = _read_pairs_memory(plan.workspace_path)
        memory_section = f"PROJECT MEMORY:\n{memory_text}\n\n" if memory_text else ""

        kb_section = knowledge_retriever.search_for_context(goal)
        kb_text = f"\n\n{kb_section}" if kb_section else ""

        raw = (
            f"CODEBASE ARCHITECTURE:\n{arch_text}\n\n"
            f"{adr_text}"
            f"{memory_section}"
            f"OPEN ISSUES: {todo_count} TODO/FIXME/HACK markers in codebase\n\n"
            f"SESSION GOAL: {goal}\n"
            f"COMPLETED TASKS: {completed_str}"
            f"{kb_text}"
        )
        return self._govern_context(
            raw=raw,
            workspace_path=plan.workspace_path,
            session_id=plan.id,
            stage="planning",
            query=goal,
        )

    # ------------------------------------------------------------------
    # Annotation context — function-scoped, import-aware, risk-labelled
    # ------------------------------------------------------------------

    def build_annotation_context(self, task: Task, plan: PlanDocument) -> str:
        target_code = self._get_target_code(task)
        outbound_text = self._get_outbound(task)
        callers_text, risk_summary = self._get_inbound_with_risk(task)
        pending_changes = self._get_pending_changes()
        completed = plan.completed_tasks()
        completed_str = ", ".join(t.title for t in completed) or "none"

        kb_section = knowledge_retriever.search_for_context(task.description)
        kb_text = f"\n\n{kb_section}" if kb_section else ""

        raw = (
            f"TASK: {task.description}\n\n"
            f"TARGET FILE: {task.target_file}\n"
            f"TARGET FUNCTION: {task.target_function or '(not specified)'}\n\n"
            f"CURRENT CODE:\n```\n{target_code}\n```\n\n"
            f"CALLS (outbound, depth 2):\n{outbound_text}\n\n"
            f"CALLERS (inbound, blast radius):\n{callers_text}\n"
            f"{risk_summary}"
            f"\nUNCOMMITTED CHANGES IN TREE:\n{pending_changes}\n\n"
            f"SESSION: {plan.goal_statement}\n"
            f"COMPLETED: {completed_str}"
            f"{kb_text}"
        )
        return self._govern_context(
            raw=raw,
            workspace_path=plan.workspace_path,
            session_id=plan.id,
            stage="annotation",
            query=task.description,
        )

    # ------------------------------------------------------------------
    # Execution context — approved intent + full file contents to write
    # ------------------------------------------------------------------

    def build_execution_context(self, task: Task, plan: PlanDocument) -> str:
        approved = [a for a in task.annotations if a.status == AnnotationStatus.APPROVED]

        annotation_lines = []
        touched_files: set[str] = set()
        for a in approved:
            annotation_lines.append(
                f"FILE: {a.target_file}\n"
                f"FUNCTION: {a.target_function or '(file-level)'}\n"
                f"WHAT: {a.summary}\n"
                f"HOW: {a.detail}\n"
                f"CREATES: {', '.join(a.will_create) or 'nothing'}\n"
                f"MODIFIES: {', '.join(a.will_modify) or 'nothing'}\n"
                f"SIDE EFFECTS: {', '.join(a.side_effect_warnings) or 'none'}"
            )
            touched_files.update(a.will_modify)
            touched_files.update(a.will_create)

        file_contents = []
        for fpath in sorted(touched_files):
            content = _read_file(fpath)
            if content is not None:
                file_contents.append(f"--- {fpath} ---\n{content}")

        raw = (
            f"APPROVED INTENT:\n{'=' * 40}\n"
            + "\n\n".join(annotation_lines)
            + f"\n\nCURRENT FILE CONTENTS:\n{'=' * 40}\n"
            + "\n\n".join(file_contents)
        )
        return self._govern_context(
            raw=raw,
            workspace_path=plan.workspace_path,
            session_id=plan.id,
            stage="execution",
            query=task.description,
        )

    # ------------------------------------------------------------------
    # Scan context — post-execution ripple analysis via detect_changes
    # ------------------------------------------------------------------

    def build_scan_context(self, task: Task) -> str:
        """
        Maps uncommitted changes (written by the just-executed task) to the full
        blast radius via detect_changes. Feeds RIPPLE_DETECTION prompt.
        """
        try:
            result = self._g.detect_changes(scope="unstaged", depth=3)
        except Exception as e:
            log.warning("build_scan_context: detect_changes failed: %s", e)
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

    # ------------------------------------------------------------------
    # Question context — minimal
    # ------------------------------------------------------------------

    def build_question_context(self, task: Optional[Task], plan: PlanDocument) -> str:
        task_str = (
            f"CURRENT TASK: {task.title}\n{task.description}" if task
            else "CURRENT TASK: none"
        )
        raw = f"SESSION GOAL: {plan.goal_statement}\n\n{task_str}"
        return self._govern_context(
            raw=raw,
            workspace_path=plan.workspace_path,
            session_id=plan.id,
            stage="question",
            query=task.description if task else plan.goal_statement,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_target_code(self, task: Task) -> str:
        """
        Retrieve the exact body of the target function via get_code_snippet.
        Falls back to reading the raw file (capped at 3000 chars) if the graph
        can't find it.
        """
        if not task.target_function:
            if task.target_file:
                return (_read_file(task.target_file) or "")[:3000]
            return "(no target specified)"

        # Try graph: find qualified name then fetch snippet
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
            log.debug("_get_target_code graph lookup failed: %s", e)

        # Fallback: raw file read
        if task.target_file:
            return (_read_file(task.target_file) or "")[:3000]
        return "(could not retrieve code)"

    def _get_outbound(self, task: Task) -> str:
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
            log.debug("_get_outbound failed: %s", e)
            return "(graph query failed)"

    def _get_inbound_with_risk(self, task: Task) -> tuple[str, str]:
        """
        Who calls the target function? Returns (callers_text, risk_summary).
        risk_labels=True adds CRITICAL/HIGH/MEDIUM/LOW per caller hop depth.
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
            log.debug("_get_inbound_with_risk failed: %s", e)
            return "(graph query failed)", ""

    def _get_pending_changes(self) -> str:
        """List files already modified in the working tree before the annotation is written."""
        try:
            result = self._g.detect_changes(scope="unstaged", depth=1)
            files = result.get("changed_files", [])
            if not files:
                return "(clean working tree)"
            return "\n".join(f"  {f}" for f in files[:10])
        except Exception:
            return "(git unavailable)"

    def _govern_context(
        self,
        raw: str,
        workspace_path: str,
        session_id: str,
        stage: str,
        query: str,
    ) -> str:
        if not workspace_path or not session_id:
            return raw
        try:
            result = self._lifecycle.govern(
                workspace_path=workspace_path,
                session_id=session_id,
                stage=stage,
                query=query,
                raw_context=raw,
            )
            log.debug("context lifecycle (%s): %s", stage, result.stats)
            return result.context
        except Exception as e:
            log.debug("context lifecycle failed (%s): %s", stage, e)
            return raw


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------

def _format_architecture(arch: dict) -> str:
    parts: list[str] = []

    langs = arch.get("languages")
    if langs:
        lang_str = ", ".join(
            f"{l['name']} ({l.get('file_count', '?')} files)"
            for l in (langs if isinstance(langs, list) else [])
        )
        parts.append(f"Languages: {lang_str}")

    entry_points = arch.get("entry_points") or []
    if entry_points:
        ep_names = [ep.get("name") or ep.get("qualified_name", "?") for ep in entry_points[:5]]
        parts.append(f"Entry points: {', '.join(ep_names)}")

    layers = arch.get("layers") or []
    if layers:
        layer_names = [la.get("name", str(la)) for la in layers[:6]]
        parts.append(f"Layers: [{', '.join(layer_names)}]")

    hotspots = arch.get("hotspots") or []
    if hotspots:
        hs_lines = [
            f"  {h.get('name', '?')} ({h.get('in_degree', '?')} callers)"
            for h in hotspots[:8]
        ]
        parts.append("Hotspots (most-called):\n" + "\n".join(hs_lines))

    clusters = arch.get("clusters") or []
    if clusters:
        cl_lines = []
        for c in clusters[:6]:
            members = c.get("members") or []
            sample = ", ".join(m.get("name", str(m)) for m in members[:4])
            ellipsis = "…" if len(members) > 4 else ""
            cl_lines.append(f"  Cluster {c.get('id', '?')}: {sample}{ellipsis}")
        parts.append("Functional clusters (Louvain):\n" + "\n".join(cl_lines))

    return "\n".join(parts) if parts else "(no architecture data)"


def _format_adr(adr: Optional[dict]) -> str:
    if not adr:
        return ""
    text = adr.get("text", "")
    if not text:
        sections = adr.get("sections", {})
        text = "\n\n".join(
            f"## {k}\n{v}" for k, v in sections.items() if v
        )
    if not text:
        return ""
    return f"ARCHITECTURE DECISION RECORD:\n{text}\n\n"


def _read_file(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_pairs_memory(workspace_path: str) -> Optional[str]:
    """Read .waterfree/memory.md if it exists — project notes that persist across sessions."""
    memory_path = Path(workspace_path) / ".waterfree" / "memory.md"
    try:
        text = memory_path.read_text(encoding="utf-8", errors="replace").strip()
        return text if text else None
    except OSError:
        return None
