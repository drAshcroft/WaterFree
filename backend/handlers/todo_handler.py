"""Todo/task-store handlers: listTasks, searchTasks, addTask, updateTask, deleteTask, whatNext."""
from __future__ import annotations

import os


def handle_list_tasks(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    store = server._get_task_store(workspace_path)
    data = store.list_tasks(
        status=str(params.get("status", "")),
        owner_name=str(params.get("ownerName", "")),
        owner_type=str(params.get("ownerType", "")),
        priority=str(params.get("priority", "")),
        phase=str(params.get("phase", "")),
        ready_only=bool(params.get("readyOnly", False)),
        limit=int(params.get("limit", 100)),
    )
    payload = data.to_dict()
    payload["path"] = store.path
    return payload


def handle_search_tasks(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    store = server._get_task_store(workspace_path)
    tasks = store.search_tasks(
        query=str(params.get("query", "")),
        limit=int(params.get("limit", 20)),
    )
    return {
        "tasks": [task.to_dict() for task in tasks],
        "count": len(tasks),
        "path": store.path,
    }


def handle_add_task(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    store = server._get_task_store(workspace_path)
    task_input = params.get("task", params)
    task = store.add_task(task_input)
    return {"task": task.to_dict(), "path": store.path}


def handle_update_task(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    store = server._get_task_store(workspace_path)
    task_id = str(params.get("taskId", ""))
    if not task_id:
        raise ValueError("taskId is required")
    task = store.update_task(task_id, params.get("patch", {}))
    return {"task": task.to_dict(), "path": store.path}


def handle_delete_task(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    store = server._get_task_store(workspace_path)
    task_id = str(params.get("taskId", ""))
    if not task_id:
        raise ValueError("taskId is required")
    deleted = store.delete_task(task_id)
    return {"ok": True, "deleted": deleted, "taskId": task_id, "path": store.path}


def handle_what_next(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    store = server._get_task_store(workspace_path)
    task = store.get_next_task(
        owner_name=str(params.get("ownerName", "")),
        include_unassigned=bool(params.get("includeUnassigned", True)),
    )
    return {"task": task.to_dict() if task else None, "path": store.path}
