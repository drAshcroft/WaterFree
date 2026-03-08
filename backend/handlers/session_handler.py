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
