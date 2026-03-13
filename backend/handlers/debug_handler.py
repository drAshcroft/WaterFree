"""Debug-related handlers: liveDebug."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.debug.live_debug import DebugContext
from backend.session.models import SessionNote

log = logging.getLogger("waterfree.server")


def handle_live_debug(server, params: dict) -> dict:
    debug_ctx_dict = params["debugContext"]
    workspace_path = params.get("workspacePath", ".")
    session_id = params.get("sessionId")

    debug_ctx = DebugContext.from_dict({
        **debug_ctx_dict,
        "workspacePath": workspace_path,
    })
    context_str = debug_ctx.format_for_llm()

    doc = server._sessions.get(session_id) if session_id else None
    persona = doc.persona if doc else "default"

    runtime = server._get_runtime_for_session(doc) if doc else server._get_runtime(workspace_path)
    analysis = runtime.analyze_debug_context(
        context_str,
        workspace_path=workspace_path,
        persona=persona,
    )

    # If there's an active session, attach a note
    if session_id:
        doc = server._sessions.get(session_id)
        if doc:
            doc.notes.append(SessionNote(
                timestamp=datetime.now(timezone.utc).isoformat(),
                author="ai",
                text=f"Live debug analysis: {analysis.get('diagnosis', '')}",
            ))
            sm = server._get_session_manager(doc.workspace_path)
            sm.save_session(doc)

    return analysis
