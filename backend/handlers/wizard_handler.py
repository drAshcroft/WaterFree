"""Wizard-related handlers."""
from __future__ import annotations

import os

from backend.session.models import TaskStatus


def handle_create_wizard_session(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    goal = str(params.get("goal", "")).strip()
    public_docs_path = str(params.get("publicDocsPath", "docs")).strip() or "docs"
    wizard_id = str(params.get("wizardId", "bring_idea_to_life")).strip() or "bring_idea_to_life"
    persona = str(params.get("persona", "architect")).strip() or "architect"
    manager = server._get_wizard_manager(workspace_path, public_docs_path=public_docs_path)
    run = manager.create_or_resume_run(goal=goal, wizard_id=wizard_id, persona=persona)
    return {
        "wizard": run.to_dict(),
        "openDocPath": manager.active_doc_path(run),
    }


def handle_get_wizard_session(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    run_id = str(params.get("runId", "")).strip() or None
    public_docs_path = str(params.get("publicDocsPath", "docs")).strip() or "docs"
    manager = server._get_wizard_manager(workspace_path, public_docs_path=public_docs_path)
    run = manager.get_run(run_id)
    if not run:
        return {"wizard": None, "openDocPath": ""}
    return {
        "wizard": run.to_dict(),
        "openDocPath": manager.active_doc_path(run),
    }


def handle_run_wizard_step(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    run_id = str(params.get("runId", "")).strip()
    stage_id = str(params.get("stageId", "")).strip()
    if not run_id or not stage_id:
        raise ValueError("runId and stageId are required")
    manager = server._get_wizard_manager(workspace_path)
    runtime = server._get_runtime(workspace_path)
    return manager.run_stage(
        run_id=run_id,
        stage_id=stage_id,
        runtime=runtime,
        revision_note=str(params.get("revisionNote", "")).strip(),
        chunk_id=str(params.get("chunkId", "")).strip(),
        extra_context=str(params.get("extraContext", "")).strip(),
    )


def handle_accept_wizard_chunk(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    run_id = str(params.get("runId", "")).strip()
    stage_id = str(params.get("stageId", "")).strip()
    chunk_id = str(params.get("chunkId", "")).strip()
    if not run_id or not stage_id or not chunk_id:
        raise ValueError("runId, stageId, and chunkId are required")
    manager = server._get_wizard_manager(workspace_path)
    return manager.accept_chunk(run_id=run_id, stage_id=stage_id, chunk_id=chunk_id)


def handle_accept_wizard_step(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    run_id = str(params.get("runId", "")).strip()
    stage_id = str(params.get("stageId", "")).strip()
    if not run_id or not stage_id:
        raise ValueError("runId and stageId are required")
    manager = server._get_wizard_manager(workspace_path)
    return manager.accept_stage(run_id=run_id, stage_id=stage_id)


def handle_start_wizard_coding(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    run_id = str(params.get("runId", "")).strip()
    if not run_id:
        raise ValueError("runId is required")
    manager = server._get_wizard_manager(workspace_path)
    session_manager = server._get_session_manager(workspace_path)
    task_store = server._get_task_store(workspace_path)
    return manager.start_coding(
        run_id=run_id,
        session_manager=session_manager,
        sessions=server._sessions,
        task_store=task_store,
    )


def handle_promote_wizard_todos(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    run_id = str(params.get("runId", "")).strip()
    if not run_id:
        raise ValueError("runId is required")
    manager = server._get_wizard_manager(workspace_path)
    task_store = server._get_task_store(workspace_path)
    return manager.promote_todos(run_id=run_id, task_store=task_store)


def handle_run_wizard_review(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    run_id = str(params.get("runId", "")).strip()
    if not run_id:
        raise ValueError("runId is required")
    manager = server._get_wizard_manager(workspace_path)
    run = manager.load_run(run_id)
    review_stage = manager.ensure_review_stage(run)

    extra_context = ""
    if run.linked_session_id and run.linked_session_id in server._sessions:
        session = server._sessions[run.linked_session_id]
        pending = [task.title for task in session.tasks if task.status != TaskStatus.COMPLETE]
        extra_context = "Linked session pending tasks:\n" + "\n".join(f"- {title}" for title in pending)

    runtime = server._get_runtime(workspace_path)
    return manager.run_stage(
        run_id=run_id,
        stage_id=review_stage.id,
        runtime=runtime,
        extra_context=extra_context,
    )
