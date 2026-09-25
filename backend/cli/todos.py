"""`waterfree todos ...` — workspace task backlog CLI.

Backed by backend.todo.store.TaskStore; mirrors the former waterfree-todos MCP.
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace, _SubParsersAction
from enum import Enum
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
from backend.session.models import (
    CoordAnchorType,
    DependencyType,
    OwnerType,
    TaskPriority,
    TaskStatus,
    TaskTiming,
    TaskType,
)
from backend.todo.store import (
    SEARCH_MODES,
    DuplicateKeyError,
    TaskNotFoundError,
    TaskSearchResult,
    TaskStore,
    normalize_search_text,
)

# Statuses/priorities accepted by the discrete `update` flags, derived from the
# enums so a new member widens the CLI automatically rather than silently failing
# argparse validation until someone notices the literal list is stale.
#
# Canonical values only: `parse_enum` additionally accepts the aliases in
# backend.session.enum_coercion (so `--patch '{"status":"completed"}'` works while
# `--status completed` does not). That asymmetry is deliberate — the JSON API is
# lenient about what it ingests, while the flag surface advertises exactly one
# spelling per value, because argparse renders these choices in `--help` and a
# list containing both "complete" and "completed" reads like two distinct states.
_STATUSES = tuple(member.value for member in TaskStatus)
_PRIORITIES = tuple(member.value for member in TaskPriority)
_ENUM_TYPES: dict[str, type[Enum]] = {
    "priority": TaskPriority,
    "status": TaskStatus,
    "taskType": TaskType,
    "timing": TaskTiming,
    "owner.type": OwnerType,
    "dependsOn[].type": DependencyType,
}

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


# Fields a search summary row quotes a snippet from, most descriptive first.
# `title` is already on the row, so it is only reported as "title" with no text.
_SNIPPET_FIELDS = (
    "description", "rationale", "acceptanceCriteria", "trigger",
    "aiNotes", "humanNotes", "key", "phase",
)
_SNIPPET_WIDTH = 120


def _field_text(task: dict, field: str) -> str:
    if field == "targetCoord.file":
        coord = task.get("targetCoord") or {}
        return str(coord.get("file") or "")
    if field == "owner.name":
        owner = task.get("owner") or {}
        return str(owner.get("name") or "")
    value = task.get(field)
    return "" if value is None else str(value)


def _match_snippet(task: dict, terms: list[str]) -> str:
    """Name the first field a term matched and quote a window of text around it."""
    if not terms:
        return ""
    if any(term in normalize_search_text(task.get("title")) for term in terms):
        return "title"
    for field in (*_SNIPPET_FIELDS, "targetCoord.file", "owner.name"):
        text = " ".join(_field_text(task, field).split())
        haystack = normalize_search_text(text)
        positions = [haystack.find(term) for term in terms]
        hits = [pos for pos in positions if pos >= 0]
        if not hits:
            continue
        # normalize_search_text only swaps characters 1:1, so offsets line up.
        start = max(0, min(hits) - _SNIPPET_WIDTH // 4)
        window = text[start:start + _SNIPPET_WIDTH]
        prefix = "…" if start > 0 else ""
        suffix = "…" if start + _SNIPPET_WIDTH < len(text) else ""
        return f"{field}: {prefix}{window}{suffix}"
    return ""


def _search_row(task: dict, terms: list[str]) -> dict:
    """The compact search shape: enough to pick a task, never its body."""
    row: dict[str, Any] = {"id": task.get("id")}
    if task.get("key"):
        row["key"] = task["key"]
    row["title"] = task.get("title")
    row["status"] = task.get("status")
    row["priority"] = task.get("priority")
    if task.get("phase"):
        row["phase"] = task["phase"]
    owner = task.get("owner") or {}
    if owner.get("name"):
        row["owner"] = owner["name"]
    snippet = _match_snippet(task, terms)
    if snippet:
        row["match"] = snippet
    return row


def _search_envelope(result: TaskSearchResult, *, full: bool, limit: int) -> dict[str, Any]:
    tasks = [task.to_dict() for task in result.tasks]
    presented = [_present(t, True) for t in tasks] if full else [_search_row(t, result.terms) for t in tasks]
    envelope: dict[str, Any] = {
        "tasks": presented,
        "total": result.total,
        "returned": len(presented),
        "mode": result.mode,
        "terms": result.terms,
    }
    if result.total > len(presented):
        envelope["truncated"] = True
        envelope["hint"] = f"{result.total} tasks match; showing {len(presented)}. Raise --limit (was {limit})."
    elif result.total == 0 and result.terms:
        if result.mode == "phrase":
            envelope["hint"] = (
                "0 tasks contain that exact phrase; drop --phrase to match the words in any order."
            )
        elif len(result.terms) > 1 and result.any_term_total:
            envelope["hint"] = (
                f"0 tasks contain all {len(result.terms)} terms; {result.any_term_total} contain at least one. "
                "Try fewer or different words."
            )
        else:
            envelope["hint"] = "0 tasks match. Search is across every text field; try a different word."
    return envelope


def _append_note(existing: str | None, addition: str) -> str:
    base = (existing or "").rstrip()
    return f"{base}\n\n{addition}" if base else addition


def _warn_if_note_shrinks(field: str, existing: str | None, replacement: str, flag: str) -> None:
    before = len(existing or "")
    if before and len(replacement) < before / 2:
        sys.stderr.write(
            f"warning: {field} shrank from {before} to {len(replacement)} characters; "
            f"earlier notes were replaced. Use --{flag} to keep them.\n"
        )


def _tasks_envelope(tasks: list[dict], *, full: bool, **extra: Any) -> dict[str, Any]:
    presented = [_present(task, full) for task in tasks]
    return {"tasks": presented, "total": len(presented), **extra}


def _enum_values(enum_type: type[Enum]) -> list[str]:
    return [str(member.value) for member in enum_type]


def _validation_message(message: str) -> str:
    """Add actionable enum choices to Python's otherwise terse enum errors."""
    for field, enum_type in _ENUM_TYPES.items():
        if enum_type.__name__ in message:
            values = ", ".join(_enum_values(enum_type))
            return f"{message}; valid {field} values: {values}"
    return message


def _task_schema() -> dict[str, Any]:
    enum_schema = {
        field: {"type": "string", "enum": _enum_values(enum_type)}
        for field, enum_type in _ENUM_TYPES.items()
        if "[]" not in field and "." not in field
    }
    coord_schema = {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "class": {"type": ["string", "null"]},
            "method": {"type": ["string", "null"]},
            "line": {"type": ["integer", "null"]},
            "anchorType": {"type": "string", "enum": _enum_values(CoordAnchorType)},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "WaterFree task",
        "type": "object",
        "required": ["title", "description"],
        "properties": {
            "id": {"type": "string", "readOnly": True},
            "key": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "rationale": {"type": "string"},
            "targetCoord": coord_schema,
            "contextCoords": {"type": "array", "items": {"$ref": "#/$defs/codeCoord"}},
            "priority": enum_schema["priority"],
            "phase": {"type": ["string", "null"]},
            "dependsOn": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "taskId": {"type": "string"},
                        "key": {"type": "string"},
                        "type": {"type": "string", "enum": _enum_values(DependencyType)},
                    },
                },
            },
            "blockedReason": {"type": ["string", "null"]},
            "owner": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": _enum_values(OwnerType)},
                    "name": {"type": "string"},
                    "assignedAt": {"type": ["string", "null"]},
                },
            },
            "taskType": enum_schema["taskType"],
            "estimatedMinutes": {"type": ["integer", "null"]},
            "actualMinutes": {"type": ["integer", "null"]},
            "status": enum_schema["status"],
            "humanNotes": {"type": ["string", "null"]},
            "aiNotes": {"type": ["string", "null"]},
            "annotations": {"type": "array"},
            "startedAt": {"type": ["string", "null"]},
            "completedAt": {"type": ["string", "null"]},
            "acceptanceCriteria": {"type": ["string", "null"]},
            "trigger": {"type": ["string", "null"]},
            "timing": enum_schema["timing"],
        },
        "$defs": {"codeCoord": coord_schema},
    }


def _add_full_flag(parser) -> None:
    parser.add_argument(
        "--full",
        action="store_true",
        help="Emit every field including nulls/defaults (default output is compact).",
    )


def _read_json_input(source: str, *, label: str) -> tuple[Any | None, str | None]:
    """Read UTF-8 JSON from a path or stdin, returning a user-facing error."""
    try:
        if source == "-":
            # TextIOWrapper's encoding is inherited from the calling console.  In
            # particular, a frozen Python process launched by Windows PowerShell
            # can decode a UTF-8 pipe with a legacy code page.  Read bytes so the
            # documented stdin contract is independent of that ambient setting.
            stream = getattr(sys.stdin, "buffer", None)
            raw = stream.read() if stream is not None else sys.stdin.read()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8-sig")
            else:
                raw.encode("utf-8")
        else:
            raw = Path(source).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return None, f"--{label} could not be read: {exc}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"--{label} must be valid JSON: {exc}"
    try:
        # json.loads accepts escaped surrogate code points.  SQLite cannot bind
        # them, so reject them before both dry-run and persistence paths.
        json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        return None, f"--{label} must contain valid UTF-8 Unicode: {exc}"
    return parsed, None


def _read_json_object(source: str, *, label: str) -> tuple[dict[str, Any] | None, str | None]:
    parsed, error = _read_json_input(source, label=label)
    if error:
        return None, error
    if not isinstance(parsed, dict):
        return None, f"--{label} must contain a JSON object"
    return parsed, None


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
    p_list.add_argument("--limit", type=int, default=50,
                        help="Page size (default 50). 'total' always reports the unpaged count.")
    p_list.add_argument("--offset", type=int, default=0, help="Skip this many tasks (paging).")
    _add_full_flag(p_list)

    p_search = actions.add_parser(
        "search",
        help="Search every text field of every task. Words match in any order; "
             "quote a run of words to require them contiguous.",
    )
    add_workspace_arg(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument(
        "--phrase", action="store_true",
        help="Match the whole query as one contiguous phrase instead of separate words.",
    )
    p_search.add_argument(
        "--full", action="store_true",
        help="Emit whole task objects. Default rows carry id, key, title, status, "
             "priority and a match snippet only.",
    )

    p_get = actions.add_parser("get", help="Fetch one task by id or key")
    add_workspace_arg(p_get)
    p_get.add_argument("task_ref", help="Task id (uuid) or stable key (e.g. GOV-001).")
    _add_full_flag(p_get)

    p_next = actions.add_parser("get-next", help="Highest-priority unblocked task")
    add_workspace_arg(p_next)
    p_next.add_argument("--owner", default="")
    _add_full_flag(p_next)

    p_ready = actions.add_parser("get-ready", help="All unblocked tasks ordered by priority")
    add_workspace_arg(p_ready)
    p_ready.add_argument("--limit", type=int, default=20)
    _add_full_flag(p_ready)

    p_schema = actions.add_parser("schema", help="Print the task JSON schema and enum values")
    add_workspace_arg(p_schema)

    p_task_types = actions.add_parser("task-types", help="List accepted taskType values")
    add_workspace_arg(p_task_types)

    p_validate = actions.add_parser("validate", help="Validate the persisted task backlog")
    add_workspace_arg(p_validate)

    p_add = actions.add_parser("add", help="Add a new task")
    add_workspace_arg(p_add)
    p_add.add_argument("--title", default=None)
    p_add.add_argument("--description", default=None)
    p_add.add_argument("--key", default=None, help="Stable identifier (e.g. GOV-001) for cross-task references.")
    p_add.add_argument("--priority", default=None)
    p_add.add_argument("--phase", default=None)
    p_add.add_argument("--owner-name", default=None)
    p_add.add_argument("--owner-type", default=None,
                       choices=("human", "agent", "unassigned"))
    p_add.add_argument("--target-file", default=None)
    p_add.add_argument("--target-line", type=int, default=None)
    p_add.add_argument(
        "--json-file", default=None,
        help="Read a complete task object from a JSON file; use '-' for stdin.",
    )
    _add_full_flag(p_add)

    p_update = actions.add_parser(
        "update",
        help="Update an existing task. Prefer the discrete flags; --patch is for "
             "fields without a flag.",
    )
    add_workspace_arg(p_update)
    p_update.add_argument("task_id", help="Task id (uuid) or stable key (e.g. GOV-001).")
    # Discrete flags cover the common writes without JSON — no shell-quoting pain.
    p_update.add_argument("--status", choices=_STATUSES, default=None)
    p_update.add_argument("--priority", choices=_PRIORITIES, default=None)
    p_update.add_argument("--phase", default=None)
    p_update.add_argument("--owner-type", choices=("human", "agent", "unassigned"), default=None)
    p_update.add_argument("--owner-name", default=None)
    ai_notes = p_update.add_mutually_exclusive_group()
    ai_notes.add_argument(
        "--ai-notes", default=None,
        help="Replace aiNotes wholesale (warns when the earlier notes were longer).",
    )
    ai_notes.add_argument(
        "--append-ai-notes", default=None, metavar="AI_NOTES",
        help="Add a paragraph to aiNotes, keeping what is already there.",
    )
    human_notes = p_update.add_mutually_exclusive_group()
    human_notes.add_argument(
        "--human-notes", default=None,
        help="Replace humanNotes wholesale (warns when the earlier notes were longer).",
    )
    human_notes.add_argument(
        "--append-human-notes", default=None, metavar="HUMAN_NOTES",
        help="Add a paragraph to humanNotes, keeping what is already there.",
    )
    p_update.add_argument("--actual-minutes", type=int, default=None)
    p_update.add_argument(
        "--patch", default=None,
        help="JSON object for fields without a discrete flag. Discrete flags win on conflict.",
    )
    p_update.add_argument(
        "--patch-file", default=None,
        help="Read a JSON patch object from a file; use '-' for stdin.",
    )
    _add_full_flag(p_update)

    p_delete = actions.add_parser("delete", help="Remove a task by id or key")
    add_workspace_arg(p_delete)
    p_delete.add_argument("task_id", help="Task id (uuid) or stable key (e.g. GOV-001).")

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
    action = args.action

    if action == "schema":
        emit_json(_task_schema())
        return EXIT_OK

    if action == "task-types":
        emit_json({"taskTypes": _enum_values(TaskType)})
        return EXIT_OK

    store = TaskStore(resolve_workspace(args))

    if action == "list":
        filters = dict(
            status=args.status,
            owner_name=args.owner,
            priority=args.priority,
            phase=args.phase,
            ready_only=args.ready_only,
        )
        data = store.list_tasks(**filters, limit=args.limit, offset=args.offset)
        total = store.count_tasks(**filters)
        envelope = _tasks_envelope([t.to_dict() for t in data.tasks], full=args.full, phases=data.phases)
        returned = envelope["total"]
        envelope["total"] = total
        envelope["returned"] = returned
        envelope["offset"] = args.offset
        envelope["limit"] = args.limit
        truncated = args.offset + returned < total
        envelope["truncated"] = truncated
        if truncated:
            envelope["nextOffset"] = args.offset + returned
            sys.stderr.write(
                f"note: {total} tasks match but only {returned} were returned "
                f"(--limit {args.limit}, --offset {args.offset}); pass a larger --limit "
                f"or --offset {args.offset + returned} for the rest.\n"
            )
        emit_json(envelope)
        return EXIT_OK

    if action == "search":
        mode = "phrase" if args.phrase else "terms"
        result = store.search_tasks_result(args.query, limit=args.limit, mode=mode)
        emit_json(_search_envelope(result, full=args.full, limit=args.limit))
        return EXIT_OK

    if action == "get":
        task = store.get_task(args.task_ref)
        if task is None:
            return emit_error(f"Task not found: {args.task_ref}", exit_code=EXIT_NOT_FOUND)
        emit_json(_present(task.to_dict(), args.full))
        return EXIT_OK

    if action == "get-next":
        task = store.get_next_task(owner_name=args.owner, include_unassigned=True)
        emit_json(_present(task.to_dict(), args.full) if task else None)
        return EXIT_OK

    if action == "get-ready":
        tasks = store.get_ready_tasks()[: args.limit]
        emit_json(_tasks_envelope([t.to_dict() for t in tasks], full=args.full))
        return EXIT_OK

    if action == "validate":
        result = store.validate()
        emit_json(result.to_dict())
        return EXIT_OK if result.ok else EXIT_USAGE

    if action == "add":
        if args.json_file is not None:
            payload, error = _read_json_object(args.json_file, label="json-file")
            if error:
                return emit_error(error, exit_code=EXIT_USAGE)
            assert payload is not None
        else:
            payload = {}
        if args.title is not None:
            payload["title"] = args.title
        if args.description is not None:
            payload["description"] = args.description
        if "title" not in payload:
            return emit_error("add requires --title or a title in --json-file", exit_code=EXIT_USAGE)
        if "description" not in payload:
            return emit_error(
                "add requires --description or a description in --json-file",
                exit_code=EXIT_USAGE,
            )
        if args.key is not None:
            payload["key"] = args.key
        if args.priority is not None:
            payload["priority"] = args.priority
        if args.phase is not None:
            payload["phase"] = args.phase
        if args.owner_type is not None or args.owner_name is not None:
            owner = dict(payload.get("owner") or {})
            if args.owner_type is not None:
                owner["type"] = args.owner_type
            if args.owner_name is not None:
                owner["name"] = args.owner_name
            payload["owner"] = owner
        elif args.json_file is None:
            payload["owner"] = {"type": "unassigned", "name": ""}
        if args.target_file is not None:
            coord: dict[str, Any] = {"file": args.target_file, "anchorType": "modify"}
            if args.target_line is not None:
                coord["line"] = args.target_line
            payload["targetCoord"] = coord
        try:
            task = store.add_task(payload)
        except DuplicateKeyError as exc:
            return emit_error(str(exc), exit_code=EXIT_USAGE)
        except ValueError as exc:
            return emit_error(_validation_message(str(exc)), exit_code=EXIT_USAGE)
        emit_json(_present(task.to_dict(), args.full))
        return EXIT_OK

    if action == "update":
        patch: dict[str, Any] = {}
        if args.patch_file is not None:
            patch_file, error = _read_json_object(args.patch_file, label="patch-file")
            if error:
                return emit_error(error, exit_code=EXIT_USAGE)
            assert patch_file is not None
            patch.update(patch_file)
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
        existing = store.get_task(args.task_id)
        if existing is None:
            return emit_error(f"Task not found: {args.task_id}", exit_code=EXIT_NOT_FOUND)
        if args.ai_notes is not None:
            _warn_if_note_shrinks("aiNotes", existing.ai_notes, args.ai_notes, "append-ai-notes")
            patch["aiNotes"] = args.ai_notes
        if args.append_ai_notes is not None:
            patch["aiNotes"] = _append_note(existing.ai_notes, args.append_ai_notes)
        if args.human_notes is not None:
            _warn_if_note_shrinks("humanNotes", existing.human_notes, args.human_notes, "append-human-notes")
            patch["humanNotes"] = args.human_notes
        if args.append_human_notes is not None:
            patch["humanNotes"] = _append_note(existing.human_notes, args.append_human_notes)
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
            task = store.update_task(task_id=existing.id, patch=patch)
        except TaskNotFoundError as exc:
            return emit_error(str(exc), exit_code=EXIT_NOT_FOUND)
        except DuplicateKeyError as exc:
            return emit_error(str(exc), exit_code=EXIT_USAGE)
        except ValueError as exc:
            return emit_error(_validation_message(str(exc)), exit_code=EXIT_USAGE)
        emit_json(_present(task.to_dict(), args.full))
        return EXIT_OK

    if action == "delete":
        existing = store.get_task(args.task_id)
        deleted = store.delete_task(existing.id) if existing else False
        emit_json({"deleted": deleted, "task_id": existing.id if existing else args.task_id})
        return EXIT_OK if deleted else EXIT_NOT_FOUND

    if action == "import":
        parsed, error = _read_json_input(args.file, label="file")
        if error:
            return emit_error(error, exit_code=EXIT_USAGE)
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
            "errors": [
                {**error, "error": _validation_message(error["error"])}
                for error in result.errors
            ],
            "dryRun": args.dry_run,
        })
        return EXIT_OK if not result.errors else EXIT_USAGE

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)
