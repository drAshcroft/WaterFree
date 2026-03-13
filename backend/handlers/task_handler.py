"""Task/annotation/execution handlers for the planning workflow."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from backend.llm.context_builder import ContextBuilder
from backend.negotiation.negotiation_controller import NegotiationController
from backend.negotiation.turn_manager import TurnManager, InvalidTransitionError
from backend.session.models import (
    AIState, AnnotationStatus, PlanDocument, SessionNote, TaskStatus,
)

log = logging.getLogger("waterfree.server")


def handle_generate_plan(server, params: dict) -> dict:
    goal = params["goal"]
    session_id = params.get("sessionId")
    workspace_path = params.get("workspacePath", ".")

    # Get or create session
    doc = server._sessions.get(session_id) if session_id else None
    if not doc:
        sm = server._get_session_manager(workspace_path)
        doc = sm.create_session(goal)
        server._sessions[doc.id] = doc

    # Ensure the graph is indexed (index_repository is idempotent / incremental).
    try:
        status = server._graph.index_status(repo_path=workspace_path)
        if status.get("status") == "not_indexed":
            log.info("No graph index found, indexing now...")
            server._graph.index(os.path.abspath(workspace_path))
    except Exception as e:
        log.warning("Graph index check failed: %s — attempting index", e)
        try:
            server._graph.index(os.path.abspath(workspace_path))
        except Exception as e2:
            log.error("Graph indexing failed: %s", e2)

    ctx = ContextBuilder(server._graph)
    context_str = ctx.build_planning_context(goal, doc)

    runtime = server._get_runtime_for_session(doc)
    tasks, questions = runtime.generate_plan(
        goal,
        context_str,
        workspace_path=workspace_path,
        persona=doc.persona,
    )

    doc.tasks = tasks
    for t in tasks:
        # Associate tasks with workspace
        if not t.target_file.startswith("/"):
            t.target_file = os.path.join(workspace_path, t.target_file)

    sm = server._get_session_manager(workspace_path)
    sm.save_session(doc)

    return {
        "sessionId": doc.id,
        "tasks": [t.to_dict() for t in tasks],
        "questions": questions,
    }


def handle_generate_annotation(server, params: dict) -> dict:
    session_id = params["sessionId"]
    task_id = params["taskId"]

    doc = server._get_session(session_id)
    if not doc:
        raise ValueError(f"Session not found: {session_id}")

    task = next((t for t in doc.tasks if t.id == task_id), None)
    if not task:
        raise ValueError(f"Task not found: {task_id}")

    ctx = ContextBuilder(server._graph)
    context_str = ctx.build_annotation_context(task, doc)

    runtime = server._get_runtime_for_session(doc)
    annotation = runtime.generate_annotation(
        task,
        context_str,
        workspace_path=doc.workspace_path,
        persona=doc.persona,
    )
    task.annotations.append(annotation)
    task.status = TaskStatus.NEGOTIATING

    sm = server._get_session_manager(doc.workspace_path)
    sm.save_session(doc)

    return annotation.to_dict()


def handle_approve_annotation(server, params: dict) -> dict:
    session_id = params["sessionId"]
    annotation_id = params["annotationId"]

    doc = server._get_session(session_id)
    if not doc:
        raise ValueError(f"Session not found: {session_id}")

    for task in doc.tasks:
        for ann in task.annotations:
            if ann.id == annotation_id:
                ann.status = AnnotationStatus.APPROVED
                sm = server._get_session_manager(doc.workspace_path)
                sm.save_session(doc)
                return {"ok": True, "annotationId": annotation_id}

    raise ValueError(f"Annotation not found: {annotation_id}")


def handle_alter_annotation(server, params: dict) -> dict:
    session_id = params["sessionId"]
    task_id = params["taskId"]
    annotation_id = params["annotationId"]
    feedback = params["feedback"]

    doc = server._require_session(session_id)
    sm = server._get_session_manager(doc.workspace_path)
    ctrl = NegotiationController(
        doc,
        TurnManager(doc, sm),
        sm,
        server._get_runtime_for_session(doc),
        ContextBuilder(server._graph),
    )
    return ctrl.alter_annotation(task_id, annotation_id, feedback)


def handle_finalize_execution(server, params: dict) -> dict:
    session_id = params["sessionId"]
    task_id = params["taskId"]
    diagnostics = list(params.get("diagnostics", []))

    doc = server._require_session(session_id)
    task = next((t for t in doc.tasks if t.id == task_id), None)
    if not task:
        raise ValueError(f"Task not found: {task_id}")

    sm = server._get_session_manager(doc.workspace_path)
    tm = TurnManager(doc, sm)

    blocking = [
        diag for diag in diagnostics
        if str(diag.get("severity", "")).lower() == "error"
    ]

    if blocking:
        from backend.server import _blocking_diagnostic_summary, _blocking_diagnostic_details
        task.status = TaskStatus.NEGOTIATING
        task.blocked_reason = _blocking_diagnostic_summary(blocking[0], len(blocking))
        doc.notes.append(SessionNote(
            timestamp=datetime.now(timezone.utc).isoformat(),
            author="system",
            text=(
                f"Execution blocked for '{task.title}': "
                f"{_blocking_diagnostic_details(blocking)}"
            ),
        ))
        tm.force(AIState.AWAITING_REVIEW)
        sm.save_session(doc)
        return {
            "ok": False,
            "status": task.status.value,
            "blockingDiagnostics": blocking,
        }

    task.status = TaskStatus.COMPLETE
    task.blocked_reason = None
    task.completed_at = datetime.now(timezone.utc).isoformat()
    doc.notes.append(SessionNote(
        timestamp=task.completed_at,
        author="system",
        text=f"Execution finalized cleanly for '{task.title}'.",
    ))
    try:
        tm.finish()
    except InvalidTransitionError:
        tm.force(AIState.IDLE)
    runtime = server._get_runtime_for_session(doc)
    flush_session = getattr(runtime, "flush_session", None)
    profile = server._get_provider_profile(doc.workspace_path)
    if callable(flush_session) and profile.policies.flush_on_task_complete:
        flush_session(task.id)
    sm.save_session(doc)
    return {
        "ok": True,
        "status": task.status.value,
        "blockingDiagnostics": [],
    }


def handle_redirect_task(server, params: dict) -> dict:
    session_id = params["sessionId"]
    task_id = params["taskId"]
    instruction = params["instruction"]

    doc = server._require_session(session_id)
    sm = server._get_session_manager(doc.workspace_path)
    ctrl = NegotiationController(
        doc,
        TurnManager(doc, sm),
        sm,
        server._get_runtime_for_session(doc),
        ContextBuilder(server._graph),
    )
    return ctrl.redirect_task(task_id, instruction)


def handle_skip_task(server, params: dict) -> dict:
    session_id = params["sessionId"]
    task_id = params["taskId"]

    doc = server._require_session(session_id)
    sm = server._get_session_manager(doc.workspace_path)
    ctrl = NegotiationController(
        doc,
        TurnManager(doc, sm),
        sm,
        server._get_runtime_for_session(doc),
        ContextBuilder(server._graph),
    )
    return ctrl.skip_task(task_id)


def handle_execute_task(server, params: dict) -> dict:
    """
    Execute an approved annotation through the active runtime and return the
    edit list to the TypeScript side for application via
    vscode.workspace.applyEdit (no files are written by the backend).
    """
    session_id = params["sessionId"]
    task_id = params["taskId"]

    doc = server._require_session(session_id)
    sm = server._get_session_manager(doc.workspace_path)

    task = next((t for t in doc.tasks if t.id == task_id), None)
    if not task:
        raise ValueError(f"Task not found: {task_id}")

    approved = [a for a in task.annotations if a.status == AnnotationStatus.APPROVED]
    if not approved:
        raise ValueError(
            f"Task '{task.title}' has no approved annotation. "
            "Approve an annotation before calling executeTask."
        )

    tm = TurnManager(doc, sm)

    try:
        tm.begin_execution()
    except InvalidTransitionError:
        tm.force(AIState.EXECUTING)

    try:
        ctx = ContextBuilder(server._graph)
        context_str = ctx.build_execution_context(task, doc)

        task.status = TaskStatus.EXECUTING
        task.blocked_reason = None
        if not task.started_at:
            task.started_at = datetime.now(timezone.utc).isoformat()

        runtime = server._get_runtime_for_session(doc)
        edits = runtime.execute_task(
            task,
            context_str,
            workspace_path=doc.workspace_path,
            persona=doc.persona,
        )

        doc.notes.append(SessionNote(
            timestamp=datetime.now(timezone.utc).isoformat(),
            author="ai",
            text=(
                f"Task executed: '{task.title}' — "
                f"{len(edits)} file edit(s) returned to editor for apply/verification."
            ),
        ))

        tm.begin_scan()
        scan_context = ctx.build_scan_context(task)
        scan_analysis = runtime.detect_ripple(
            task,
            scan_context,
            workspace_path=doc.workspace_path,
        )
        if scan_analysis:
            doc.notes.append(SessionNote(
                timestamp=datetime.now(timezone.utc).isoformat(),
                author="ai",
                text=f"Ripple scan: {scan_analysis}",
            ))

        sm.save_session(doc)

        log.info("executeTask: task '%s' generated %d edit(s)", task.title, len(edits))
        return {
            "edits": edits,
            "taskId": task_id,
            "sessionId": session_id,
            "rippleScan": scan_analysis or "",
        }

    except Exception:
        task.status = TaskStatus.NEGOTIATING
        tm.force(AIState.IDLE)
        raise


def handle_queue_todo_instruction(server, params: dict) -> dict:
    file_ = params["file"]
    line = int(params.get("line", 0))
    instruction = params["instruction"]
    session_id = params.get("sessionId")
    workspace_path = os.path.abspath(params.get("workspacePath", "."))

    doc: Optional[PlanDocument] = None
    session_result = {"queued": True, "taskId": None}
    if session_id:
        doc = server._require_session(session_id)
        workspace_path = doc.workspace_path
        sm = server._get_session_manager(doc.workspace_path)
        ctrl = NegotiationController(
            doc,
            TurnManager(doc, sm),
            sm,
            server._get_runtime_for_session(doc),
            ContextBuilder(server._graph),
        )
        session_result = ctrl.queue_todo_instruction(file_, line, instruction)

    store = server._get_task_store(workspace_path)
    task = store.queue_todo(file_path=file_, line=line, instruction=instruction)
    return {
        **session_result,
        "backlogTaskId": task.id,
        "task": task.to_dict(),
        "path": store.path,
    }
