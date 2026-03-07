"""
NegotiationController — orchestrates alter/redirect/skip/todo-queue operations.

Each method modifies the session document and saves it.
The TurnManager enforces state validity before and after each operation.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from backend.session.models import (
    AnnotationStatus, SessionNote, Task, TaskStatus, PlanDocument,
)

if TYPE_CHECKING:
    from backend.llm.claude_client import ClaudeClient
    from backend.llm.context_builder import ContextBuilder
    from backend.negotiation.turn_manager import TurnManager
    from backend.session.session_manager import SessionManager

log = logging.getLogger(__name__)


class NegotiationController:
    def __init__(
        self,
        doc: PlanDocument,
        turn_manager: "TurnManager",
        session_manager: "SessionManager",
        claude: "ClaudeClient",
        context_builder: "ContextBuilder",
    ):
        self._doc = doc
        self._tm = turn_manager
        self._sm = session_manager
        self._claude = claude
        self._ctx = context_builder

    # ------------------------------------------------------------------
    # Alter — revise an annotation based on developer feedback
    # ------------------------------------------------------------------

    def alter_annotation(self, task_id: str, annotation_id: str, feedback: str) -> dict:
        """
        Replace an existing annotation with a revised one incorporating feedback.
        Returns the new annotation dict.
        """
        task = self._require_task(task_id)
        old_ann = self._require_annotation(task, annotation_id)

        self._tm.start_annotating()
        try:
            context_str = self._ctx.build_annotation_context(task, self._doc)
            new_ann = self._claude.alter_annotation(
                task,
                old_ann,
                feedback,
                context_str,
                workspace_path=self._doc.workspace_path,
                persona=self._doc.persona,
            )

            # Mark old as altered, append new as pending
            old_ann.status = AnnotationStatus.ALTERED
            task.annotations.append(new_ann)

            self._tm.annotation_ready()
            self._add_note(f"Annotation altered for task '{task.title}': {feedback}")
            self._sm.save_session(self._doc)
            return new_ann.to_dict()
        except Exception:
            self._tm.force(__import__("backend.session.models", fromlist=["AIState"]).AIState.IDLE)
            raise

    # ------------------------------------------------------------------
    # Redirect — discard plan fragment, apply new instruction
    # ------------------------------------------------------------------

    def redirect_task(self, task_id: str, instruction: str) -> dict:
        """
        Clear all annotations for a task and update its description with a new direction.
        The task goes back to 'pending' so a fresh annotation can be generated.
        Returns the updated task dict.
        """
        task = self._require_task(task_id)

        # Mark all annotations as redirected
        for ann in task.annotations:
            ann.status = AnnotationStatus.REDIRECTED

        # Update task with the new direction
        task.description = f"{task.description}\n\nREDIRECT: {instruction}"
        task.status = TaskStatus.PENDING

        self._add_note(f"Task '{task.title}' redirected: {instruction}")
        self._tm.force(__import__("backend.session.models", fromlist=["AIState"]).AIState.IDLE)
        self._sm.save_session(self._doc)
        return task.to_dict()

    # ------------------------------------------------------------------
    # Skip — mark task as skipped, move to next
    # ------------------------------------------------------------------

    def skip_task(self, task_id: str) -> dict:
        """
        Mark a task as skipped without executing it.
        Returns the updated session dict.
        """
        task = self._require_task(task_id)
        task.status = TaskStatus.SKIPPED
        self._add_note(f"Task skipped: '{task.title}'")
        self._sm.save_session(self._doc)
        return self._doc.to_dict()

    # ------------------------------------------------------------------
    # TODO queue — treat [wf] comments as pending instructions
    # ------------------------------------------------------------------

    def queue_todo_instruction(self, file: str, line: int, instruction: str) -> dict:
        """
        Record a [wf] TODO comment as a session note.
        If a current task is active, appends the instruction to its description.
        Returns {queued: True, taskId: str|None}.
        """
        note_text = f"[wf] TODO at {file}:{line} — {instruction}"
        self._add_note(note_text)

        # Attach to the current active/pending task if one exists
        current = self._doc.current_task()
        if current and current.status in (TaskStatus.PENDING, TaskStatus.NEGOTIATING):
            current.description += f"\n\nDeveloper note: {instruction}"
            self._sm.save_session(self._doc)
            return {"queued": True, "taskId": current.id}

        self._sm.save_session(self._doc)
        return {"queued": True, "taskId": None}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_task(self, task_id: str) -> Task:
        task = next((t for t in self._doc.tasks if t.id == task_id), None)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        return task

    def _require_annotation(self, task: Task, annotation_id: str):
        ann = next((a for a in task.annotations if a.id == annotation_id), None)
        if not ann:
            raise ValueError(f"Annotation not found: {annotation_id}")
        return ann

    def _add_note(self, text: str) -> None:
        self._doc.notes.append(SessionNote(
            timestamp=datetime.now(timezone.utc).isoformat(),
            author="system",
            text=text,
        ))
