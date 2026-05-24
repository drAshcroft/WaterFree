"""`waterfree todos ...` — workspace task backlog CLI.

Backed by backend.todo.store.TaskStore; mirrors the former waterfree-todos MCP.
"""

from __future__ import annotations

from argparse import Namespace, _SubParsersAction
from typing import Any

from backend.cli._common import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    add_workspace_arg,
    emit_error,
    emit_json,
    parse_json_arg,
    resolve_workspace,
)
from backend.todo.store import TaskStore


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("todos", help="Workspace task backlog")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_list = actions.add_parser("list", help="List tasks with optional filters")
    add_workspace_arg(p_list)
    p_list.add_argument("--status", default="")
    p_list.add_argument("--priority", default="")
    p_list.add_argument("--phase", default="")
    p_list.add_argument("--owner", default="")
    p_list.add_argument("--ready-only", action="store_true")
    p_list.add_argument("--limit", type=int, default=50)

    p_search = actions.add_parser("search", help="Full-text search across tasks")
    add_workspace_arg(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)

    p_next = actions.add_parser("get-next", help="Highest-priority unblocked task")
    add_workspace_arg(p_next)
    p_next.add_argument("--owner", default="")

    p_ready = actions.add_parser("get-ready", help="All unblocked tasks ordered by priority")
    add_workspace_arg(p_ready)
    p_ready.add_argument("--limit", type=int, default=20)

    p_add = actions.add_parser("add", help="Add a new task")
    add_workspace_arg(p_add)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--description", required=True)
    p_add.add_argument("--priority", default="P2")
    p_add.add_argument("--phase", default="")
    p_add.add_argument("--owner-name", default="")
    p_add.add_argument("--owner-type", default="unassigned",
                       choices=("human", "agent", "unassigned"))
    p_add.add_argument("--target-file", default="")
    p_add.add_argument("--target-line", type=int, default=None)

    p_update = actions.add_parser("update", help="Patch fields on an existing task")
    add_workspace_arg(p_update)
    p_update.add_argument("task_id")
    p_update.add_argument("--patch", required=True,
                          help="JSON object of fields to update")

    p_delete = actions.add_parser("delete", help="Remove a task by id")
    add_workspace_arg(p_delete)
    p_delete.add_argument("task_id")

    p.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    store = TaskStore(resolve_workspace(args))
    action = args.action

    if action == "list":
        data = store.list_tasks(
            status=args.status,
            owner_name=args.owner,
            priority=args.priority,
            phase=args.phase,
            ready_only=args.ready_only,
            limit=args.limit,
        )
        emit_json({
            "tasks": [t.to_dict() for t in data.tasks],
            "phases": data.phases,
            "total": len(data.tasks),
        })
        return EXIT_OK

    if action == "search":
        tasks = store.search_tasks(query=args.query, limit=args.limit)
        emit_json([t.to_dict() for t in tasks])
        return EXIT_OK

    if action == "get-next":
        task = store.get_next_task(owner_name=args.owner, include_unassigned=True)
        emit_json(task.to_dict() if task else None)
        return EXIT_OK

    if action == "get-ready":
        tasks = store.get_ready_tasks()[: args.limit]
        emit_json([t.to_dict() for t in tasks])
        return EXIT_OK

    if action == "add":
        payload: dict[str, Any] = {
            "title": args.title,
            "description": args.description,
            "priority": args.priority,
            "owner": {"type": args.owner_type, "name": args.owner_name},
        }
        if args.phase:
            payload["phase"] = args.phase
        if args.target_file:
            coord: dict[str, Any] = {"file": args.target_file, "anchorType": "modify"}
            if args.target_line is not None:
                coord["line"] = args.target_line
            payload["targetCoord"] = coord
        task = store.add_task(payload)
        emit_json(task.to_dict())
        return EXIT_OK

    if action == "update":
        patch = parse_json_arg(args.patch, label="patch")
        if not isinstance(patch, dict):
            return emit_error("--patch must be a JSON object", exit_code=EXIT_USAGE)
        try:
            task = store.update_task(task_id=args.task_id, patch=patch)
        except ValueError as exc:
            return emit_error(str(exc), exit_code=EXIT_NOT_FOUND)
        emit_json(task.to_dict())
        return EXIT_OK

    if action == "delete":
        deleted = store.delete_task(args.task_id)
        emit_json({"deleted": deleted, "task_id": args.task_id})
        return EXIT_OK if deleted else EXIT_NOT_FOUND

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)
