"""
Workspace task/backlog tool descriptors.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from backend.todo.store import TaskStore

from .types import ToolDescriptor, ToolPolicy

log = logging.getLogger(__name__)


def task_tool_descriptors(
    task_store_factory: Callable[[str], TaskStore],
) -> list[ToolDescriptor]:
    stores: dict[str, TaskStore] = {}

    def store_for(args: dict, workspace_path: str) -> TaskStore:
        candidate = str(args.get("workspacePath", "") or workspace_path)
        resolved = str(Path(candidate).resolve())
        if resolved not in stores:
            stores[resolved] = task_store_factory(resolved)
        return stores[resolved]

    def list_tasks(args: dict, workspace_path: str) -> dict:
        store = store_for(args, workspace_path)
        data = store.list_tasks(
            status=str(args.get("status", "")),
            owner_name=str(args.get("ownerName", "")),
            owner_type=str(args.get("ownerType", "")),
            priority=str(args.get("priority", "")),
            phase=str(args.get("phase", "")),
            ready_only=bool(args.get("readyOnly", False)),
            limit=int(args.get("limit", 100)),
        )
        payload = data.to_dict()
        payload["path"] = store.path
        return payload

    def search_tasks(args: dict, workspace_path: str) -> dict:
        store = store_for(args, workspace_path)
        tasks = store.search_tasks(
            query=str(args.get("query", "")),
            limit=int(args.get("limit", 20)),
        )
        return {"tasks": [task.to_dict() for task in tasks], "count": len(tasks), "path": store.path}

    def add_task(args: dict, workspace_path: str) -> dict:
        store = store_for(args, workspace_path)
        payload = {k: v for k, v in args.items() if k != "workspacePath"}
        task = store.add_task(payload)
        return {"task": task.to_dict(), "path": store.path}

    def update_task(args: dict, workspace_path: str) -> dict:
        store = store_for(args, workspace_path)
        task = store.update_task(
            str(args.get("taskId", "")),
            dict(args.get("patch", {})),
        )
        return {"task": task.to_dict(), "path": store.path}

    def delete_task(args: dict, workspace_path: str) -> dict:
        store = store_for(args, workspace_path)
        deleted = store.delete_task(str(args.get("taskId", "")))
        return {"ok": True, "deleted": deleted, "path": store.path}

    def what_next(args: dict, workspace_path: str) -> dict:
        store = store_for(args, workspace_path)
        task = store.get_next_task(
            owner_name=str(args.get("ownerName", "")),
            include_unassigned=bool(args.get("includeUnassigned", True)),
        )
        return {"task": task.to_dict() if task else None, "path": store.path}

    specs = [
        (
            "list_tasks",
            "List durable workspace backlog tasks from .waterfree/tasks.db.",
            {
                "type": "object",
                "properties": {
                    "workspacePath": {"type": "string"},
                    "status": {"type": "string"},
                    "ownerName": {"type": "string"},
                    "ownerType": {"type": "string"},
                    "priority": {"type": "string"},
                    "phase": {"type": "string"},
                    "readyOnly": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
            },
        ),
        (
            "search_tasks",
            "Search the durable workspace backlog by title, description, or target path.",
            {
                "type": "object",
                "properties": {
                    "workspacePath": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        ),
        (
            "add_task",
            "Add a task to the durable workspace backlog.",
            {"type": "object", "properties": {"workspacePath": {"type": "string"}}},
        ),
        (
            "update_task",
            "Update fields on an existing backlog task.",
            {
                "type": "object",
                "properties": {
                    "workspacePath": {"type": "string"},
                    "taskId": {"type": "string"},
                    "patch": {"type": "object"},
                },
                "required": ["taskId", "patch"],
            },
        ),
        (
            "delete_task",
            "Delete a task from the durable workspace backlog.",
            {
                "type": "object",
                "properties": {"workspacePath": {"type": "string"}, "taskId": {"type": "string"}},
                "required": ["taskId"],
            },
        ),
        (
            "what_next",
            "Return the highest-priority ready backlog task.",
            {
                "type": "object",
                "properties": {
                    "workspacePath": {"type": "string"},
                    "ownerName": {"type": "string"},
                    "includeUnassigned": {"type": "boolean"},
                },
            },
        ),
    ]
    handlers = {
        "list_tasks": list_tasks,
        "search_tasks": search_tasks,
        "add_task": add_task,
        "update_task": update_task,
        "delete_task": delete_task,
        "what_next": what_next,
    }

    policy = ToolPolicy(read_only=False, requires_approval=False, category="backlog")
    read_only = {"list_tasks", "search_tasks", "what_next"}
    descriptors: list[ToolDescriptor] = []
    for name, description, schema in specs:
        p = policy if name not in read_only else ToolPolicy(read_only=True, category="backlog")
        descriptors.append(
            ToolDescriptor(
                name=name,
                title=name.replace("_", " "),
                description=description,
                input_schema=schema,
                handler=handlers[name],
                policy=p,
                server_id="waterfree-todos",
            )
        )
    return descriptors
