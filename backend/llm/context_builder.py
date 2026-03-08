"""
ContextBuilder — assembles per-turn LLM context strings.

Delegates to:
  GraphQueryBuilder  — codebase graph queries
  context_formatter  — text formatting, file reads, doc excerpting
  ContextLifecycleManager — token budget governance
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.graph.client import GraphClient
from backend.knowledge import retriever as knowledge_retriever  # noqa: F401 — re-exported for test patching
from backend.llm.context_formatter import (
    build_design_inputs,
    read_file,
    search_knowledge,
)
from backend.llm.context_lifecycle import ContextLifecycleManager
from backend.llm.graph_query_builder import GraphQueryBuilder
from backend.session.models import AnnotationStatus, PlanDocument, Task

log = logging.getLogger(__name__)


class ContextBuilder:
    def __init__(self, graph: GraphClient):
        self._gqb = GraphQueryBuilder(graph)
        self._lifecycle = ContextLifecycleManager()

    # -- Planning --------------------------------------------------------------

    def build_planning_context(self, goal: str, plan: PlanDocument) -> str:
        arch_text, adr_text = self._gqb.get_architecture()
        todo_count = self._gqb.count_open_todos()
        completed = plan.completed_tasks()
        completed_str = ", ".join(t.title for t in completed) or "none yet"

        design_inputs = build_design_inputs(
            workspace_path=plan.workspace_path,
            session_goal=goal,
            query=goal,
        )
        kb_text = search_knowledge(goal)

        raw = (
            f"CODEBASE ARCHITECTURE:\n{arch_text}\n\n"
            f"{adr_text}"
            f"{design_inputs}"
            f"OPEN ISSUES: {todo_count} TODO/FIXME/HACK markers in codebase\n\n"
            f"SESSION GOAL: {goal}\n"
            f"COMPLETED TASKS: {completed_str}"
            f"{kb_text}"
        )
        return self._govern_context(raw=raw, plan=plan, stage="planning", query=goal)

    # -- Annotation ------------------------------------------------------------

    def build_annotation_context(self, task: Task, plan: PlanDocument) -> str:
        target_code = self._gqb.get_target_code(task)
        outbound_text = self._gqb.get_outbound_calls(task)
        callers_text, risk_summary = self._gqb.get_inbound_with_risk(task)
        pending_changes = self._gqb.get_pending_changes()
        completed = plan.completed_tasks()
        completed_str = ", ".join(t.title for t in completed) or "none"

        design_query = " ".join(
            part for part in [plan.goal_statement, task.title, task.description] if part
        )
        design_inputs = build_design_inputs(
            workspace_path=plan.workspace_path,
            session_goal=plan.goal_statement,
            current_task=task,
            query=design_query,
        )
        kb_text = search_knowledge(task.description)

        raw = (
            f"TASK: {task.description}\n\n"
            f"TARGET FILE: {task.target_file}\n"
            f"TARGET FUNCTION: {task.target_function or '(not specified)'}\n\n"
            f"CURRENT CODE:\n```\n{target_code}\n```\n\n"
            f"{design_inputs}"
            f"CALLS (outbound, depth 2):\n{outbound_text}\n\n"
            f"CALLERS (inbound, blast radius):\n{callers_text}\n"
            f"{risk_summary}"
            f"\nUNCOMMITTED CHANGES IN TREE:\n{pending_changes}\n\n"
            f"SESSION: {plan.goal_statement}\n"
            f"COMPLETED: {completed_str}"
            f"{kb_text}"
        )
        return self._govern_context(raw=raw, plan=plan, stage="annotation", query=task.description)

    # -- Execution -------------------------------------------------------------

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
            content = read_file(fpath)
            if content is not None:
                file_contents.append(f"--- {fpath} ---\n{content}")

        raw = (
            f"APPROVED INTENT:\n{'=' * 40}\n"
            + "\n\n".join(annotation_lines)
            + f"\n\nCURRENT FILE CONTENTS:\n{'=' * 40}\n"
            + "\n\n".join(file_contents)
        )
        return self._govern_context(raw=raw, plan=plan, stage="execution", query=task.description)

    # -- Scan ------------------------------------------------------------------

    def build_scan_context(self, task: Task) -> str:
        return self._gqb.get_scan_context(task)

    # -- Question --------------------------------------------------------------

    def build_question_context(self, task: Optional[Task], plan: PlanDocument) -> str:
        task_str = (
            f"CURRENT TASK: {task.title}\n{task.description}" if task
            else "CURRENT TASK: none"
        )
        raw = f"SESSION GOAL: {plan.goal_statement}\n\n{task_str}"
        query = task.description if task else plan.goal_statement
        return self._govern_context(raw=raw, plan=plan, stage="question", query=query)

    # -- Governance ------------------------------------------------------------

    def _govern_context(self, *, raw: str, plan: PlanDocument, stage: str, query: str) -> str:
        if not plan.workspace_path or not plan.id:
            return raw
        try:
            result = self._lifecycle.govern(
                workspace_path=plan.workspace_path,
                session_id=plan.id,
                stage=stage,
                query=query,
                raw_context=raw,
            )
            log.debug("context lifecycle (%s): %s", stage, result.stats)
            return result.context
        except Exception as e:
            log.debug("context lifecycle failed (%s): %s", stage, e)
            return raw
