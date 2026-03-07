"""
MCP server — workspace task / todo tools.

Exposes TaskStore as MCP tools so Claude Code and other MCP clients can read,
create, and update tasks in the workspace backlog at .waterfree/tasks.db.

Run:
    python -m backend.mcp_todos

Register with Claude Code:
    claude mcp add pairprogram-todos python -- -m backend.mcp_todos
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from backend.mcp_logging import configure_mcp_logger, instrument_tool
from backend.todo.store import TaskStore

mcp = FastMCP("pairprogram-todos")
log, LOG_FILE = configure_mcp_logger("pairprogram-todos")

# One TaskStore per workspace path, lazily created.
_stores: dict[str, TaskStore] = {}


def _store(workspace_path: str) -> TaskStore:
    if workspace_path not in _stores:
        _stores[workspace_path] = TaskStore(workspace_path)
    return _stores[workspace_path]


def _task_to_dict(task) -> dict:
    return task.to_dict()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _list_tasks_impl(
    workspace_path: str,
    status: str = "",
    priority: str = "",
    phase: str = "",
    owner: str = "",
    ready_only: bool = False,
    limit: int = 50,
) -> str:
    """List tasks in the workspace backlog with optional filters.

    Args:
        workspace_path: Absolute path to the project root.
        status: Filter by status — 'pending', 'ready', 'executing', 'complete', 'skipped'.
        priority: Filter by priority — 'P0', 'P1', 'P2', 'P3', 'spike'.
        phase: Filter by milestone/phase name.
        owner: Filter by owner name (case-insensitive).
        ready_only: If true, only return tasks with no blocking dependencies.
        limit: Maximum number of tasks to return (default 50).

    Returns JSON with tasks list and phases.
    """
    store = _store(workspace_path)
    data = store.list_tasks(
        status=status,
        owner_name=owner,
        priority=priority,
        phase=phase,
        ready_only=ready_only,
        limit=limit,
    )
    return json.dumps(
        {
            "tasks": [_task_to_dict(t) for t in data.tasks],
            "phases": data.phases,
            "total": len(data.tasks),
        },
        indent=2,
    )


def _search_tasks_impl(workspace_path: str, query: str, limit: int = 20) -> str:
    """Full-text search across task titles, descriptions, rationale, file paths, and owners.

    Args:
        workspace_path: Absolute path to the project root.
        query: Search term.
        limit: Maximum results (default 20).

    Returns JSON list of matching tasks.
    """
    store = _store(workspace_path)
    tasks = store.search_tasks(query=query, limit=limit)
    return json.dumps([_task_to_dict(t) for t in tasks], indent=2)


def _get_next_task_impl(workspace_path: str, owner: str = "") -> str:
    """Get the highest-priority ready task (no blocking dependencies).

    Args:
        workspace_path: Absolute path to the project root.
        owner: If provided, prefer tasks assigned to this owner or unassigned.

    Returns JSON of the next task, or null if the backlog is clear.
    """
    store = _store(workspace_path)
    task = store.get_next_task(owner_name=owner, include_unassigned=True)
    return json.dumps(_task_to_dict(task) if task else None, indent=2)


def _add_task_impl(
    workspace_path: str,
    title: str,
    description: str,
    priority: str = "P2",
    phase: str = "",
    owner_name: str = "",
    owner_type: str = "unassigned",
    target_file: str = "",
    target_line: int | None = None,
) -> str:
    """Add a new task to the workspace backlog.

    Args:
        workspace_path: Absolute path to the project root.
        title: Short task title (one line).
        description: Detailed description of what needs to be done.
        priority: 'P0' (critical) | 'P1' | 'P2' (default) | 'P3' | 'spike'.
        phase: Optional milestone/phase name to group the task under.
        owner_name: Name of the assignee ('human', 'agent', or a name).
        owner_type: 'human' | 'agent' | 'unassigned' (default).
        target_file: Optional workspace-relative file path the task applies to.
        target_line: Optional line number within target_file.
                     Omit (or null) for top of file.
                     -1 means end of file.
                     Any positive integer pins the task to that line.

    Returns JSON of the created task including its generated ID.
    """
    store = _store(workspace_path)
    task_input: dict = {
        "title": title,
        "description": description,
        "priority": priority,
        "owner": {"type": owner_type, "name": owner_name},
    }
    if phase:
        task_input["phase"] = phase
    if target_file:
        coord: dict = {"file": target_file, "anchorType": "modify"}
        if target_line is not None:
            coord["line"] = target_line
        task_input["targetCoord"] = coord
    task = store.add_task(task_input)
    return json.dumps(_task_to_dict(task), indent=2)


def _update_task_impl(workspace_path: str, task_id: str, patch: str) -> str:
    """Update fields of an existing task.

    Args:
        workspace_path: Absolute path to the project root.
        task_id: The task's UUID.
        patch: JSON object string with fields to update. Supported keys:
               title, description, rationale, priority, phase, status,
               owner (object with type/name), blockedReason,
               humanNotes, aiNotes, estimatedMinutes, actualMinutes.
               Example: '{"status": "complete", "actualMinutes": 30}'

    Returns JSON of the updated task.
    """
    store = _store(workspace_path)
    try:
        patch_dict = json.loads(patch)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"Invalid JSON patch: {exc}"})
    task = store.update_task(task_id=task_id, patch=patch_dict)
    return json.dumps(_task_to_dict(task), indent=2)


def _delete_task_impl(workspace_path: str, task_id: str) -> str:
    """Delete a task from the workspace backlog.

    Args:
        workspace_path: Absolute path to the project root.
        task_id: The task's UUID.

    Returns JSON with 'deleted' boolean.
    """
    store = _store(workspace_path)
    deleted = store.delete_task(task_id=task_id)
    return json.dumps({"deleted": deleted, "task_id": task_id})


def _get_ready_tasks_impl(workspace_path: str, limit: int = 20) -> str:
    """List tasks that have no blocking dependencies and are ready to work on.

    Args:
        workspace_path: Absolute path to the project root.
        limit: Maximum results (default 20).

    Returns JSON list of ready tasks ordered by priority.
    """
    store = _store(workspace_path)
    tasks = store.get_ready_tasks()[:limit]
    return json.dumps([_task_to_dict(t) for t in tasks], indent=2)


list_tasks = mcp.tool()(instrument_tool(log, "list_tasks", _list_tasks_impl))
search_tasks = mcp.tool()(instrument_tool(log, "search_tasks", _search_tasks_impl))
get_next_task = mcp.tool()(instrument_tool(log, "get_next_task", _get_next_task_impl))
add_task = mcp.tool()(instrument_tool(log, "add_task", _add_task_impl))
update_task = mcp.tool()(instrument_tool(log, "update_task", _update_task_impl))
delete_task = mcp.tool()(instrument_tool(log, "delete_task", _delete_task_impl))
get_ready_tasks = mcp.tool()(instrument_tool(log, "get_ready_tasks", _get_ready_tasks_impl))


if __name__ == "__main__":
    log.info("Starting MCP server pairprogram-todos (logFile=%s)", LOG_FILE)
    mcp.run()
