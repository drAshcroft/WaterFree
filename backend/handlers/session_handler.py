"""Session-related handlers: createSession, getSession, saveSession."""
from __future__ import annotations

from typing import Optional

from backend.session.models import PlanDocument


def handle_create_session(server, params: dict) -> dict:
    goal = params["goal"]
    workspace_path = params.get("workspacePath", ".")
    persona = params.get("persona", "default")
    sm = server._get_session_manager(workspace_path)
    doc = sm.create_session(goal, persona=persona)
    server._sessions[doc.id] = doc
    return doc.to_dict()


def handle_get_session(server, params: dict) -> Optional[dict]:
    session_id = params.get("sessionId")
    if session_id:
        doc = server._sessions.get(session_id)
        return doc.to_dict() if doc else None
    # Try loading from disk
    workspace_path = params.get("workspacePath", ".")
    sm = server._get_session_manager(workspace_path)
    doc = sm.load_session()
    if doc:
        server._sessions[doc.id] = doc
        return doc.to_dict()
    return None


def handle_save_session(server, params: dict) -> dict:
    session_data = params.get("session", {})
    doc = PlanDocument.from_dict(session_data)
    server._sessions[doc.id] = doc
    sm = server._get_session_manager(doc.workspace_path)
    sm.save_session(doc)
    return {"ok": True}


def handle_list_archived_sessions(server, params: dict) -> dict:
    workspace_path = params.get("workspacePath", ".")
    sm = server._get_session_manager(workspace_path)
    return {"sessions": sm.list_archived()}


def handle_restore_session(server, params: dict) -> Optional[dict]:
    workspace_path = params.get("workspacePath", ".")
    filename = params.get("file", "")
    sm = server._get_session_manager(workspace_path)
    archive_path = sm._archive_dir / filename
    if not archive_path.exists():
        return None
    import json
    data = json.loads(archive_path.read_text())
    doc = PlanDocument.from_dict(data)
    sm.save_session(doc)
    server._sessions[doc.id] = doc
    return doc.to_dict()
