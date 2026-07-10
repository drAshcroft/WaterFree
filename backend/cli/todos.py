"""`waterfree todos ...` — workspace task backlog CLI.

Backed by backend.todo.store.TaskStore; mirrors the former waterfree-todos MCP.
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace, _SubParsersAction
from pathlib import Path
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
from backend.todo.store import DuplicateKeyError, TaskNotFoundError, TaskStore

# Statuses/priorities accepted by the discrete `update` flags. Kept in sync with
# backend.session.task_models.TaskStatus / TaskPriority.
_STATUSES = ("pending", "annotating", "negotiating", "executing", "complete", "skipped")
_PRIORITIES = ("P0", "P1", "P2", "P3", "spike")

# Field values that carry no information and are stripped from compact output.
_DEFAULT_DROP = {"timing": "one_time", "taskType": "impl"}


def _compact_coord(coord: Any) -> dict | None:
    """Drop a coord with no file; otherwise keep only the meaningful anchors."""
    if not isinstance(coord, dict):
        return None
    file = coord.get("file") or ""
    if not file:
        return None
    out: dict[str, Any] = {"file": file}
    if coord.get("line") is not None:
        out["line"] = coord["line"]
    if coord.get("class"):
        out["class"] = coord["class"]
    if coord.get("method"):
        out["method"] = coord["method"]
    anchor = coord.get("anchorType")
    if anchor and anchor != "modify":
        out["anchorType"] = anchor
    return out


def _compact_task(task: dict) -> dict:
    """Strip null/empty/default fields from a task dict to cut token cost.

    An omitted field means "empty or default": no owner means unassigned,
    no `timing` means one_time, no `taskType` means impl, etc.
    """
    out: dict[str, Any] = {}
    for key, value in task.items():
        if value in (None, "", [], {}):
            continue
        if key in _DEFAULT_DROP and value == _DEFAULT_DROP[key]:
            continue
        if key == "owner":
            if isinstance(value, dict) and value.get("type", "unassigned") == "unassigned" and not value.get("name"):
                continue
            out[key] = {k: v for k, v in value.items() if v not in (None, "")}
            continue
        if key == "targetCoord":
            coord = _compact_coord(value)
            if coord:
                out[key] = coord
            continue
        if key == "contextCoords":
            coords = [c for c in (_compact_coord(item) for item in value) if c]
            if coords:
                out[key] = coords
            continue
        out[key] = value
    return out


def _present(task: dict, full: bool) -> dict:
    return task if full else _compact_task(task)


def _add_full_flag(parser) -> None:
    parser.add_argument(
        "--full",
        action="store_true",
        help="Emit every field including nulls/defaults (default output is compact).",
    )


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
    _add_full_flag(p_list)

    p_search = actions.add_parser("search", help="Full-text search across tasks")
    add_workspace_arg(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    _add_full_flag(p_search)

    p_next = actions.add_parser("get-next", help="Highest-priority unblocked task")
    add_workspace_arg(p_next)
    p_next.add_argument("--owner", default="")
    _add_full_flag(p_next)

    p_ready = actions.add_parser("get-ready", help="All unblocked tasks ordered by priority")
    add_workspace_arg(p_ready)
    p_ready.add_argument("--limit", type=int, default=20)
    _add_full_flag(p_ready)

    p_add = actions.add_parser("add", help="Add a new task")
    add_workspace_arg(p_add)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--description", required=True)
    p_add.add_argument("--key", default="", help="Stable identifier (e.g. GOV-001) for cross-task references.")
    p_add.add_argument("--priority", default="P2")
    p_add.add_argument("--phase", default="")
    p_add.add_argument("--owner-name", default="")
    p_add.add_argument("--owner-type", default="unassigned",
                       choices=("human", "agent", "unassigned"))
    p_add.add_argument("--target-file", default="")
    p_add.add_argument("--target-line", type=int, default=None)
    _add_full_flag(p_add)

    p_update = actions.add_parser(
        "update",
        help="Update an existing task. Prefer the discrete flags; --patch is for "
             "fields without a flag.",
    )
    add_workspace_arg(p_update)
    p_update.add_argument("task_id")
    # Discrete flags cover the common writes without JSON — no shell-quoting pain.
    p_update.add_argument("--status", choices=_STATUSES, default=None)
    p_update.add_argument("--priority", choices=_PRIORITIES, default=None)
    p_update.add_argument("--phase", default=None)
    p_update.add_argument("--owner-type", choices=("human", "agent", "unassigned"), default=None)
    p_update.add_argument("--owner-name", default=None)
    p_update.add_argument("--ai-notes", default=None, help="Replace aiNotes.")
    p_update.add_argument("--human-notes", default=None, help="Replace humanNotes.")
    p_update.add_argument("--actual-minutes", type=int, default=None)
    p_update.add_argument(
        "--patch", default=None,
        help="JSON object for fields without a discrete flag. Discrete flags win on conflict.",
    )
    _add_full_flag(p_update)

    p_delete = actions.add_parser("delete", help="Remove a task by id")
    add_workspace_arg(p_delete)
    p_delete.add_argument("task_id")

    p_import = actions.add_parser(
        "import",
        help="Bulk create/update tasks from a JSON file. Validates the whole "
             "batch before writing anything.",
    )
    add_workspace_arg(p_import)
    p_import.add_argument(
        "--file", required=True,
        help="Path to a JSON file: an array of tasks, or {\"tasks\": [...]}. Use '-' for stdin.",
    )
    p_import.add_argument(
        "--upsert", action="store_true",
        help="Update existing tasks matched by key instead of erroring on collision.",
    )
    p_import.add_argument(
        "--dry-run", action="store_true",
        help="Validate only; write nothing.",
    )
    _add_full_flag(p_import)

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
            "tasks": [_present(t.to_dict(), args.full) for t in data.tasks],
            "phases": data.phases,
            "total": len(data.tasks),
        })
        return EXIT_OK

    if action == "search":
        tasks = store.search_tasks(query=args.query, limit=args.limit)
        emit_json([_present(t.to_dict(), args.full) for t in tasks])
        return EXIT_OK

    if action == "get-next":
        task = store.get_next_task(owner_name=args.owner, include_unassigned=True)
        emit_json(_present(task.to_dict(), args.full) if task else None)
        return EXIT_OK

    if action == "get-ready":
        tasks = store.get_ready_tasks()[: args.limit]
        emit_json([_present(t.to_dict(), args.full) for t in tasks])
        return EXIT_OK

    if action == "add":
        payload: dict[str, Any] = {
            "title": args.title,
            "description": args.description,
            "priority": args.priority,
            "owner": {"type": args.owner_type, "name": args.owner_name},
        }
        if args.key:
            payload["key"] = args.key
        if args.phase:
            payload["phase"] = args.phase
        if args.target_file:
            coord: dict[str, Any] = {"file": args.target_file, "anchorType": "modify"}
            if args.target_line is not None:
                coord["line"] = args.target_line
            payload["targetCoord"] = coord
        try:
            task = store.add_task(payload)
        except DuplicateKeyError as exc:
            return emit_error(str(exc), exit_code=EXIT_USAGE)
        emit_json(_present(task.to_dict(), args.full))
        return EXIT_OK

    if action == "update":
        patch: dict[str, Any] = {}
        if args.patch is not None:
            parsed = parse_json_arg(args.patch, label="patch")
            if not isinstance(parsed, dict):
                return emit_error("--patch must be a JSON object", exit_code=EXIT_USAGE)
            patch.update(parsed)
        # Discrete flags override --patch on conflict.
        if args.status is not None:
            patch["status"] = args.status
        if args.priority is not None:
            patch["priority"] = args.priority
        if args.phase is not None:
            patch["phase"] = args.phase
        if args.ai_notes is not None:
            patch["aiNotes"] = args.ai_notes
        if args.human_notes is not None:
            patch["humanNotes"] = args.human_notes
        if args.actual_minutes is not None:
            patch["actualMinutes"] = args.actual_minutes
        if args.owner_type is not None or args.owner_name is not None:
            # owner is replaced wholesale; set both flags to preserve type+name.
            patch["owner"] = {
                "type": args.owner_type or "unassigned",
                "name": args.owner_name or "",
            }
        if not patch:
            return emit_error(
                "nothing to update: pass a flag like --status/--priority or --patch",
                exit_code=EXIT_USAGE,
            )
        try:
            task = store.update_task(task_id=args.task_id, patch=patch)
        except TaskNotFoundError as exc:
            return emit_error(str(exc), exit_code=EXIT_NOT_FOUND)
        except DuplicateKeyError as exc:
            return emit_error(str(exc), exit_code=EXIT_USAGE)
        emit_json(_present(task.to_dict(), args.full))
        return EXIT_OK

    if action == "delete":
        deleted = store.delete_task(args.task_id)
        emit_json({"deleted": deleted, "task_id": args.task_id})
        return EXIT_OK if deleted else EXIT_NOT_FOUND

    if action == "import":
        if args.file == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.file).read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return emit_error(f"--file must be valid JSON: {exc}", exit_code=EXIT_USAGE)
        items = parsed.get("tasks", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(items, list):
            return emit_error(
                "--file must contain a JSON array of tasks, or an object with a 'tasks' array",
                exit_code=EXIT_USAGE,
            )
        result = store.import_tasks(items, upsert=args.upsert, dry_run=args.dry_run)
        emit_json({
            "created": [_present(t.to_dict(), args.full) for t in result.created],
            "updated": [_present(t.to_dict(), args.full) for t in result.updated],
            "errors": result.errors,
            "dryRun": args.dry_run,
        })
        return EXIT_OK if not result.errors else EXIT_USAGE

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)
