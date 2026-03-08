"""Index-related handlers: indexWorkspace, updateFile, removeFile."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("waterfree.server")


def _write_notification(method: str, params: dict) -> None:
    # Import lazily to avoid circular imports; the function lives in server.py
    from backend.server import _write_notification as _wn
    _wn(method, params)


def handle_index_workspace(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("path", "."))
    store = server._get_index_state_store(workspace_path)
    check = store.quick_check()

    needs_index = not check.has_prior_index or check.changed_count > 0
    reason = "first_index" if not check.has_prior_index else "changed_files"

    if not needs_index:
        try:
            status = server._graph.index_status(repo_path=workspace_path)
            if status.get("status") == "not_indexed":
                needs_index = True
                reason = "graph_project_missing"
        except Exception as exc:
            log.warning("Graph project check failed; forcing index: %s", exc)
            needs_index = True
            reason = "graph_check_failed"

    if not needs_index:
        return {
            "status": "up_to_date",
            "indexed": False,
            "changedCount": 0,
            "changedPaths": [],
            "workspacePath": workspace_path,
            "dbPath": store.db_path,
            "scannedFiles": check.scanned_files,
        }

    log.info(
        "Indexing workspace via graph: %s (reason=%s, changed=%d)",
        workspace_path,
        reason,
        check.changed_count,
    )
    _write_notification("indexProgress", {"done": 0, "total": 1})
    graph_result = server._graph.index(workspace_path)
    _write_notification("indexProgress", {"done": 1, "total": 1})

    store.record_index(check, reason=reason, index_result=graph_result)

    return {
        "status": "indexed",
        "indexed": True,
        "reason": reason,
        "changedCount": check.changed_count,
        "changedPaths": check.changed_paths,
        "workspacePath": workspace_path,
        "dbPath": store.db_path,
        "scannedFiles": check.scanned_files,
        "graph": graph_result,
    }


def handle_update_file(server, params: dict) -> dict:
    # codebase-memory-mcp auto-syncs on file changes via its background watcher.
    # This endpoint is kept for protocol compatibility but is now a no-op.
    return {"ok": True}


def handle_remove_file(server, params: dict) -> dict:
    return {"ok": True}
