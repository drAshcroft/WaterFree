"""`waterfree todos ...` — workspace task backlog CLI.

Backed by backend.todo.store.TaskStore; mirrors the former waterfree-todos MCP.

Output contract: every result goes to stdout as JSON. Nothing informational is
written to stderr — PowerShell turns any native stderr output into a
NativeCommandError, so a note there makes a successful call look failed.
Advisory text travels in the JSON instead (`hint` on envelopes, `warnings` on
task results); stderr carries only real errors, which also exit non-zero.
"""

from __future__ import annotations

import json
import os
import re
import sys
from argparse import Namespace, _SubParsersAction
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from backend.cli._common import (
    EXIT_INTERNAL,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    add_workspace_arg,
    emit_error,
    emit_json,
    parse_json_arg,
    resolve_workspace,
)
from backend.session.enum_coercion import parse_enum
from backend.session.models import (
    CoordAnchorType,
    DependencyType,
    OwnerType,
    Task,
    TaskPriority,
    TaskStatus,
    TaskTiming,
    TaskType,
)
from backend.session.task_models import TaskBlocker
from backend.todo import git_context
from backend.todo.git_context import TodosWorkspace, display_path, resolve_todos_workspace
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
_BLOCKERS = tuple(member.value for member in TaskBlocker)
_PRUNABLE_STATUSES = (TaskStatus.COMPLETE.value, TaskStatus.SKIPPED.value)
_ENUM_TYPES: dict[str, type[Enum]] = {
    "priority": TaskPriority,
    "status": TaskStatus,
    "taskType": TaskType,
    "timing": TaskTiming,
    "blockedOn": TaskBlocker,
    "owner.type": OwnerType,
    "dependsOn[].type": DependencyType,
}

# Field values that carry no information and are stripped from compact output.
_DEFAULT_DROP = {"timing": "one_time", "taskType": "impl"}

# `state` is meant to be read in seconds: verdict, open items, rejected
# approaches. Past this it is turning into a second log.
STATE_SOFT_CAP = 1_500
# Past this, aiNotes has become a history nobody reads end to end; `get` points
# the reader (and the next writer) at `state`.
AI_NOTES_WARN_AT = 5_000

# Actions that may create the store when a worktree redirect finds none yet.
_CREATING_ACTIONS = {"add", "import"}


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
        if key == "workRef" and isinstance(value, dict):
            out[key] = {k: v for k, v in value.items() if v not in (None, "")}
            continue
        out[key] = value
    return out


def _present(task: dict, full: bool) -> dict:
    return task if full else _compact_task(task)


def _project(task: dict, fields: list[str]) -> dict:
    """`--fields` projection: exactly the named top-level fields, nulls included."""
    return {name: task.get(name) for name in fields}


def _ref_label(task: Task) -> str:
    return task.key or task.id


# Fields a search summary row quotes a snippet from, most descriptive first.
# `title` is already on the row, so it is only reported as "title" with no text.
_SNIPPET_FIELDS = (
    "description", "rationale", "acceptanceCriteria", "trigger", "state",
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


def _search_envelope(
    result: TaskSearchResult, *, full: bool, limit: int, fields: list[str] | None = None,
) -> dict[str, Any]:
    tasks = [task.to_dict() for task in result.tasks]
    if fields:
        presented = [_project(t, fields) for t in tasks]
    elif full:
        presented = [_present(t, True) for t in tasks]
    else:
        presented = [_search_row(t, result.terms) for t in tasks]
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


def _note_shrink_warning(field: str, existing: str | None, replacement: str, flag: str) -> str | None:
    before = len(existing or "")
    if before and len(replacement) < before / 2:
        return (
            f"{field} shrank from {before} to {len(replacement)} characters; "
            f"earlier notes were replaced. Use --{flag} to keep them."
        )
    return None


def _long_notes_warning(task: Task) -> str | None:
    size = len(task.ai_notes or "")
    if size <= AI_NOTES_WARN_AT:
        return None
    ref = _ref_label(task)
    if task.state:
        return (
            f"aiNotes is {size:,} characters; read `state` first and keep it current "
            f"(verdict, open items, rejected approaches): todos update {ref} --state-file -"
        )
    return (
        f"aiNotes is {size:,} characters and this task has no `state`; update `state` with the "
        f"current verdict and rejected approaches: todos update {ref} --state-file -"
    )


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def _notes_view(task: dict, tail: int | None) -> dict:
    """`get --notes-only`: identity, state, and the notes — optionally the last N paragraphs."""
    out: dict[str, Any] = {"id": task.get("id")}
    if task.get("key"):
        out["key"] = task["key"]
    out["title"] = task.get("title")
    out["status"] = task.get("status")
    if task.get("state"):
        out["state"] = task["state"]
    truncated: dict[str, Any] = {}
    for field in ("aiNotes", "humanNotes"):
        text = task.get(field) or ""
        if not text:
            continue
        if tail is not None:
            paragraphs = _paragraphs(text)
            if len(paragraphs) > tail:
                truncated[field] = {"shown": tail, "total": len(paragraphs)}
                text = "\n\n".join(paragraphs[-tail:])
        out[field] = text
    if truncated:
        out["truncated"] = truncated
    return out


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
            "key": {"type": "string", "description": "Explicit stable identifier, separate from title. Set with add --key."},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "rationale": {"type": "string", "description": "State prerequisite mechanics or architecture work and their cost before implementation."},
            "targetCoord": coord_schema,
            "contextCoords": {"type": "array", "items": {"$ref": "#/$defs/codeCoord"}},
            "priority": enum_schema["priority"],
            "phase": {"type": ["string", "null"]},
            "dependsOn": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "taskId": {"type": "string", "description": "Task id; a task key is also resolved."},
                        "key": {"type": "string"},
                        "type": {"type": "string", "enum": _enum_values(DependencyType)},
                    },
                },
            },
            "blockedReason": {"type": ["string", "null"]},
            "blockedOn": {
                "type": ["string", "null"],
                "enum": [*_enum_values(TaskBlocker), None],
                "description": "Typed blocker: waiting on a human decision or something external. "
                               "A task with blockedOn is not handed out by get-next/get-ready.",
            },
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
            "state": {
                "type": ["string", "null"],
                "description": f"Overwritable current state in markdown (verdict / open / rejected); "
                               f"soft cap ~{STATE_SOFT_CAP:,} characters. aiNotes stays the history.",
            },
            "humanNotes": {"type": ["string", "null"]},
            "aiNotes": {"type": ["string", "null"]},
            "annotations": {"type": "array"},
            "startedAt": {"type": ["string", "null"], "description": "Stamped on the first move to executing."},
            "completedAt": {"type": ["string", "null"], "description": "Stamped on the move to complete; cleared on reopen."},
            "workRef": {
                "type": ["object", "null"],
                "description": "Where the work lives; recorded automatically by `todos update`.",
                "properties": {
                    "branch": {"type": ["string", "null"]},
                    "worktreePath": {"type": ["string", "null"]},
                    "headCommit": {"type": ["string", "null"]},
                    "recordedAt": {"type": ["string", "null"]},
                },
            },
            "acceptanceCriteria": {"type": ["string", "null"], "description": "Define observable completion criteria, including any prerequisite new mechanic."},
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


def _add_fields_flag(parser) -> None:
    parser.add_argument(
        "--fields",
        help="Comma-separated top-level task fields to emit, e.g. key,title,priority,state.",
    )


def _read_text_input(source: str, *, label: str) -> tuple[str | None, str | None]:
    """Read UTF-8 text from a path or stdin ('-'), returning a user-facing error."""
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
    return raw, None


def _read_json_input(source: str, *, label: str) -> tuple[Any | None, str | None]:
    """Read UTF-8 JSON from a path or stdin, returning a user-facing error."""
    raw, error = _read_text_input(source, label=label)
    if error:
        return None, error
    assert raw is not None
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


def _read_refs(source: str) -> tuple[list[str] | None, str | None]:
    """Task ids/keys for `delete --ids-file`.

    Plain text (one id or key per line; blank lines and `#` comments ignored) or
    JSON: an array of strings, an array of task objects, or `{"tasks": [...]}`
    — so `todos list --fields id` output can be fed straight back in.
    """
    raw, error = _read_text_input(source, label="ids-file")
    if error:
        return None, error
    assert raw is not None
    text = raw.strip()
    if text.startswith(("[", "{")):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"--ids-file looks like JSON but is not valid: {exc}"
        items = parsed.get("tasks") if isinstance(parsed, dict) else parsed
        if not isinstance(items, list):
            return None, "--ids-file JSON must be an array, or an object with a 'tasks' array"
        refs: list[str] = []
        for item in items:
            if isinstance(item, str):
                refs.append(item)
            elif isinstance(item, dict) and (item.get("id") or item.get("key")):
                refs.append(str(item.get("id") or item.get("key")))
            else:
                return None, "--ids-file entries must be ids/keys or objects with an 'id' or 'key'"
        return refs, None
    return [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ], None


_AGE_RE = re.compile(r"^\s*(\d+)\s*([dhw]?)\s*$", re.IGNORECASE)


def _parse_age(text: str) -> timedelta | None:
    """`30d`, `12h`, `2w`, or a bare number of days."""
    match = _AGE_RE.match(text or "")
    if not match:
        return None
    amount, unit = int(match.group(1)), (match.group(2) or "d").lower()
    return {"d": timedelta(days=amount), "h": timedelta(hours=amount), "w": timedelta(weeks=amount)}[unit]


def _write_export(target: Path, tasks: list[Task], workspace: str) -> int:
    """Write (or extend) a JSON export of `tasks`; returns the number written.

    An existing export is merged into, not overwritten — pruning to the same
    history file every month must not discard last month's history. The file is
    replaced atomically, and an existing file that is not a todos export is an
    error rather than something to clobber.
    """
    previous: list[dict] = []
    if target.exists():
        parsed, error = _read_json_input(str(target), label="export")
        if error:
            raise ValueError(f"{target} exists but could not be read as a todos export ({error})")
        items = parsed.get("tasks") if isinstance(parsed, dict) else parsed
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError(f"{target} exists but is not a todos export (expected a 'tasks' array)")
        previous = items
    new_ids = {task.id for task in tasks}
    merged = [item for item in previous if item.get("id") not in new_ids]
    merged.extend(task.to_dict() for task in tasks)
    payload = {
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "workspace": display_path(workspace),
        "tasks": merged,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)
    return len(tasks)


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
    p_list.add_argument(
        "--blocked", action="store_true",
        help="Only unfinished tasks that are blocked: by an incomplete dependency or a typed blocker.",
    )
    p_list.add_argument(
        "--blocked-on", choices=_BLOCKERS, default=None,
        help="Only unfinished tasks with this typed blocker.",
    )
    p_list.add_argument("--limit", type=int, default=50,
                        help="Page size (default 50). 'total' always reports the unpaged count.")
    p_list.add_argument("--offset", type=int, default=0, help="Skip this many tasks (paging).")
    _add_full_flag(p_list)
    _add_fields_flag(p_list)

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
    _add_fields_flag(p_search)

    p_get = actions.add_parser("get", help="Fetch one task by id or key")
    add_workspace_arg(p_get)
    p_get.add_argument("task_ref", help="Task id (uuid) or stable key (e.g. GOV-001).")
    _add_full_flag(p_get)
    _add_fields_flag(p_get)
    p_get.add_argument(
        "--notes-only", action="store_true",
        help="Emit only id/key/title/status, state, aiNotes and humanNotes.",
    )
    p_get.add_argument(
        "--tail", type=int, default=None, metavar="N",
        help="With --notes-only (implied): only the last N paragraphs of each notes field.",
    )

    p_next = actions.add_parser("get-next", help="Highest-priority unblocked task")
    add_workspace_arg(p_next)
    p_next.add_argument("--owner", default="")
    _add_full_flag(p_next)
    _add_fields_flag(p_next)

    p_ready = actions.add_parser("get-ready", help="All unblocked tasks ordered by priority")
    add_workspace_arg(p_ready)
    p_ready.add_argument("--limit", type=int, default=20)
    _add_full_flag(p_ready)
    _add_fields_flag(p_ready)

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
    p_update.add_argument("--key", default=None, help="Set or change the task's stable key.")
    p_update.add_argument("--title", default=None)
    p_update.add_argument("--description", default=None)
    p_update.add_argument("--acceptance-criteria", default=None)
    p_update.add_argument("--owner-type", choices=("human", "agent", "unassigned"), default=None)
    p_update.add_argument("--owner-name", default=None)
    p_update.add_argument(
        "--blocked-on", choices=_BLOCKERS, default=None,
        help="Typed blocker: waiting on a human decision or something external. "
             "Takes the task out of get-next/get-ready. Needs a --blocked-reason.",
    )
    p_update.add_argument("--blocked-reason", default=None, help="What the task is waiting for.")
    p_update.add_argument(
        "--unblock", action="store_true",
        help="Clear blockedOn and blockedReason, returning the task to the ready queue.",
    )
    state = p_update.add_mutually_exclusive_group()
    state.add_argument(
        "--state", default=None,
        help=f"Replace the task's current state (markdown: verdict / open / rejected; "
             f"~{STATE_SOFT_CAP:,} chars). Pass '' to clear.",
    )
    state.add_argument("--state-file", default=None, help="Replace state from a UTF-8 file; '-' for stdin.")
    ai_notes = p_update.add_mutually_exclusive_group()
    ai_notes.add_argument(
        "--ai-notes", default=None,
        help="Replace aiNotes wholesale (warns when the earlier notes were longer).",
    )
    ai_notes.add_argument(
        "--append-ai-notes", default=None, metavar="AI_NOTES",
        help="Add a paragraph to aiNotes, keeping what is already there.",
    )
    ai_notes.add_argument(
        "--append-ai-notes-file", default=None, metavar="FILE",
        help="Append a paragraph to aiNotes from a UTF-8 file; '-' for stdin.",
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
    human_notes.add_argument(
        "--append-human-notes-file", default=None, metavar="FILE",
        help="Append a paragraph to humanNotes from a UTF-8 file; '-' for stdin.",
    )
    p_update.add_argument("--actual-minutes", type=int, default=None)
    p_update.add_argument(
        "--branch", default=None,
        help="Record the branch the work is on (overrides the automatic workRef).",
    )
    p_update.add_argument(
        "--worktree", default=None,
        help="Record the checkout the work is in (overrides the automatic workRef).",
    )
    p_update.add_argument(
        "--patch", default=None,
        help="JSON object for fields without a discrete flag. Discrete flags win on conflict.",
    )
    p_update.add_argument(
        "--patch-file", default=None,
        help="Read a JSON patch object from a file; use '-' for stdin.",
    )
    _add_full_flag(p_update)
    p_update.add_argument("--quiet", action="store_true", help="Print only the updated task id and status.")

    p_delete = actions.add_parser(
        "delete",
        help="Remove a task by id or key, or many with --ids-file (one transaction).",
    )
    add_workspace_arg(p_delete)
    p_delete.add_argument("task_id", nargs="?", default=None, help="Task id (uuid) or stable key (e.g. GOV-001).")
    p_delete.add_argument(
        "--ids-file", default=None,
        help="Ids/keys to delete: one per line, or a JSON array / {\"tasks\": [...]}; '-' for stdin. "
             "Nothing is deleted if any entry is unknown unless --ignore-missing.",
    )
    p_delete.add_argument(
        "--ignore-missing", action="store_true",
        help="With --ids-file: delete the known tasks and report the unknown ones.",
    )
    p_delete.add_argument("--dry-run", action="store_true", help="Report what would be deleted; write nothing.")

    p_prune = actions.add_parser(
        "prune",
        help="Delete finished tasks in bulk after exporting them to a JSON file.",
    )
    add_workspace_arg(p_prune)
    p_prune.add_argument(
        "--status", action="append", choices=_PRUNABLE_STATUSES, default=None,
        help="Status to prune; repeat for both (default: complete).",
    )
    p_prune.add_argument(
        "--older-than", default=None, metavar="AGE",
        help="Only tasks whose completedAt is older than AGE (30d, 12h, 2w; bare number = days).",
    )
    p_prune.add_argument(
        "--include-undated", action="store_true",
        help="With --older-than: also prune tasks that have no completedAt.",
    )
    p_prune.add_argument(
        "--export", default=None, metavar="FILE",
        help="JSON file the pruned tasks are written to (merged into an existing export) "
             "before anything is deleted. Required unless --dry-run.",
    )
    p_prune.add_argument("--dry-run", action="store_true", help="Report what would be pruned; write nothing.")

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


class _Output:
    """Emits results, making a worktree redirect and any warnings visible in the JSON."""

    def __init__(self, workspace: TodosWorkspace):
        self.workspace = workspace

    def emit(self, payload: Any, warnings: list[str] | None = None) -> None:
        if isinstance(payload, dict):
            payload = dict(payload)
            if warnings:
                payload["warnings"] = list(warnings)
            if self.workspace.redirected:
                payload["workspace"] = display_path(self.workspace.path)
                payload["resolvedFrom"] = self.workspace.resolved_from
        emit_json(payload)

    def not_found(self, ref: str) -> int:
        message = f"Task not found: {ref}"
        if self.workspace.redirected:
            message += (
                f" (looked in the main checkout's store {display_path(self.workspace.path)}"
                f"/.waterfree/tasks.db; this worktree {display_path(self.workspace.worktree or '')}"
                " has no store of its own)"
            )
        return emit_error(message, exit_code=EXIT_NOT_FOUND)


def _no_store_message(workspace: TodosWorkspace) -> str:
    return (
        "no task store found: neither this worktree "
        f"({display_path(workspace.worktree or workspace.requested)}) nor its main checkout "
        f"({display_path(workspace.path)}) has .waterfree/tasks.db. "
        "Pass --workspace to point at the project that owns the backlog; "
        "`todos add`/`todos import` from here create it in the main checkout."
    )


def run(args: Namespace) -> int:
    action = args.action
    fields = None
    if getattr(args, 'fields', None):
        fields = [f.strip() for f in args.fields.split(',') if f.strip()]
        allowed = set(_task_schema()['properties']) | {'id'}
        invalid = set(fields) - allowed
        if not fields or invalid:
            return emit_error(f"Unknown or empty task fields: {', '.join(sorted(invalid))}. Use top-level schema fields.", exit_code=EXIT_USAGE)

    if action == "schema":
        emit_json(_task_schema())
        return EXIT_OK

    if action == "task-types":
        emit_json({"taskTypes": _enum_values(TaskType)})
        return EXIT_OK

    raw_workspace = getattr(args, "workspace", None)
    if raw_workspace and re.match(r"^[A-Za-z]:[^\\/]", raw_workspace):
        return emit_error("--workspace must be an absolute drive path (use C:/Projects/name).", exit_code=EXIT_USAGE)
    requested = resolve_workspace(args)
    if not Path(requested).is_dir():
        return emit_error(f"Workspace directory does not exist: {requested}", exit_code=EXIT_USAGE)
    workspace = resolve_todos_workspace(requested)
    if workspace.redirected and not workspace.store_exists() and action not in _CREATING_ACTIONS:
        return emit_error(_no_store_message(workspace), exit_code=EXIT_NOT_FOUND)
    store = TaskStore(workspace.path)
    try:
        return _run_store(args, store, fields, workspace)
    finally:
        store.close()


def _patched_status(existing: Task, patch: dict) -> TaskStatus | None:
    if "status" not in patch:
        return existing.status
    try:
        return parse_enum(TaskStatus, patch["status"], field="status")
    except ValueError:
        return None  # the update itself will reject it


def _auto_work_ref(existing: Task, patch: dict, workspace: str) -> dict | None:
    """The `workRef` an update should record on its own, or None.

    * Moving to `executing` records the checkout the command runs in (any
      checkout of the task store's repository) — a new start is authoritative.
    * From a linked worktree, any update to a task that is executing or being
      completed refreshes it, unless the task's workRef already names a
      *different* worktree that still exists (an agent noting something on
      another agent's task must not steal its pointer).
    * Everything else (orchestrator edits from the main checkout, notes on
      pending tasks) leaves the stored pointer alone.
    """
    new_status = _patched_status(existing, patch)
    if new_status is None:
        return None
    invocation = os.getcwd()
    if new_status == TaskStatus.EXECUTING and existing.status != TaskStatus.EXECUTING:
        return git_context.detect_work_ref(invocation, workspace)
    if new_status not in (TaskStatus.EXECUTING, TaskStatus.COMPLETE):
        return None
    if not git_context.looks_like_linked_worktree(invocation):
        return None
    ref = git_context.detect_work_ref(invocation, workspace)
    if ref is None:
        return None
    stored = existing.work_ref.worktree_path if existing.work_ref else None
    if (
        stored
        and not git_context.same_path(stored, ref["worktreePath"])
        and Path(stored).is_dir()
        and git_context.looks_like_linked_worktree(stored)
    ):
        return None
    return ref


def _run_store(args: Namespace, store: TaskStore, fields: list[str] | None, workspace: TodosWorkspace) -> int:
    action = args.action
    out = _Output(workspace)

    if action == "list":
        filters = dict(
            status=args.status,
            owner_name=args.owner,
            priority=args.priority,
            phase=args.phase,
            ready_only=args.ready_only,
            blocked=args.blocked,
            blocked_on=args.blocked_on or "",
        )
        data = store.list_tasks(**filters, limit=args.limit, offset=args.offset)
        total = store.count_tasks(**filters)
        tasks = [t.to_dict() for t in data.tasks]
        presented = [_project(t, fields) for t in tasks] if fields else [_present(t, args.full) for t in tasks]
        returned = len(presented)
        envelope: dict[str, Any] = {
            "tasks": presented,
            "total": total,
            "phases": data.phases,
            "returned": returned,
            "offset": args.offset,
            "limit": args.limit,
        }
        truncated = args.offset + returned < total
        envelope["truncated"] = truncated
        if truncated:
            next_offset = args.offset + returned
            envelope["nextOffset"] = next_offset
            envelope["hint"] = (
                f"{total} tasks match but only {returned} were returned "
                f"(--limit {args.limit}, --offset {args.offset}); pass a larger --limit "
                f"or --offset {next_offset} for the rest."
            )
        out.emit(envelope)
        return EXIT_OK

    if action == "search":
        mode = "phrase" if args.phrase else "terms"
        result = store.search_tasks_result(args.query, limit=args.limit, mode=mode)
        out.emit(_search_envelope(result, full=args.full, limit=args.limit, fields=fields))
        return EXIT_OK

    if action == "get":
        task = store.get_task(args.task_ref)
        if task is None:
            return out.not_found(args.task_ref)
        notes_only = args.notes_only or args.tail is not None
        if args.tail is not None and args.tail < 1:
            return emit_error("--tail must be 1 or more", exit_code=EXIT_USAGE)
        if notes_only and fields:
            return emit_error("--notes-only/--tail and --fields are mutually exclusive", exit_code=EXIT_USAGE)
        warning = _long_notes_warning(task)
        warnings = [warning] if warning else []
        if notes_only:
            out.emit(_notes_view(task.to_dict(), args.tail), warnings)
        elif fields:
            out.emit(_project(task.to_dict(), fields), warnings)
        else:
            payload = _present(task.to_dict(), args.full)
            if task.work_ref and not task.work_ref.is_empty():
                payload["workStatus"] = git_context.live_work_status(
                    task.work_ref.to_dict(), task_ref=_ref_label(task), workspace=workspace.path,
                )
            out.emit(payload, warnings)
        return EXIT_OK

    if action == "get-next":
        task = store.get_next_task(owner_name=args.owner, include_unassigned=True)
        if task is None:
            emit_json(None)
        else:
            out.emit(_project(task.to_dict(), fields) if fields else _present(task.to_dict(), args.full))
        return EXIT_OK

    if action == "get-ready":
        tasks = [t.to_dict() for t in store.get_ready_tasks()[: args.limit]]
        presented = [_project(t, fields) for t in tasks] if fields else [_present(t, args.full) for t in tasks]
        out.emit({"tasks": presented, "total": len(presented)})
        return EXIT_OK

    if action == "validate":
        result = store.validate()
        out.emit(result.to_dict())
        return EXIT_OK if result.ok else EXIT_USAGE

    if action == "add":
        return _run_add(args, store, out)

    if action == "update":
        return _run_update(args, store, out, workspace)

    if action == "delete":
        return _run_delete(args, store, out)

    if action == "prune":
        return _run_prune(args, store, out, workspace)

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
        out.emit({
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


def _run_add(args: Namespace, store: TaskStore, out: _Output) -> int:
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
    warnings: list[str] = []
    if not payload.get('key') and re.search(r'\[[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9-]+\]', str(payload.get('title', ''))):
        warnings.append(
            "bracketed identifiers in titles are not task keys; set --key explicitly for get/update by key."
        )
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
        task = store.add_task(payload, strict_dependencies=True)
    except DuplicateKeyError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)
    except ValueError as exc:
        return emit_error(_validation_message(str(exc)), exit_code=EXIT_USAGE)
    out.emit(_present(task.to_dict(), args.full), warnings)
    return EXIT_OK


def _run_update(args: Namespace, store: TaskStore, out: _Output, workspace: TodosWorkspace) -> int:
    stdin_readers = [
        flag for flag, value in (
            ("--patch-file", args.patch_file),
            ("--state-file", args.state_file),
            ("--append-ai-notes-file", args.append_ai_notes_file),
            ("--append-human-notes-file", args.append_human_notes_file),
        ) if value == "-"
    ]
    if len(stdin_readers) > 1:
        return emit_error(
            f"only one input can read stdin ('-'); got {', '.join(stdin_readers)}",
            exit_code=EXIT_USAGE,
        )
    if args.unblock and (args.blocked_on is not None or args.blocked_reason is not None):
        return emit_error("--unblock cannot be combined with --blocked-on/--blocked-reason", exit_code=EXIT_USAGE)

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

    existing = store.get_task(args.task_id)
    if existing is None:
        return out.not_found(args.task_id)

    # Discrete flags override --patch on conflict.
    for flag_value, field in (
        (args.status, "status"),
        (args.priority, "priority"),
        (args.phase, "phase"),
        (args.key, "key"),
        (args.title, "title"),
        (args.description, "description"),
        (args.acceptance_criteria, "acceptanceCriteria"),
    ):
        if flag_value is not None:
            patch[field] = flag_value

    if args.unblock:
        patch["blockedOn"] = None
        patch["blockedReason"] = None
    if args.blocked_reason is not None:
        patch["blockedReason"] = args.blocked_reason
    if args.blocked_on is not None:
        patch["blockedOn"] = args.blocked_on
        reason = patch["blockedReason"] if "blockedReason" in patch else existing.blocked_reason
        if not str(reason or "").strip():
            return emit_error(
                "--blocked-on needs --blocked-reason TEXT saying what the task is waiting for",
                exit_code=EXIT_USAGE,
            )

    warnings: list[str] = []
    if args.state is not None:
        patch["state"] = args.state
    if args.state_file is not None:
        text, error = _read_text_input(args.state_file, label="state-file")
        if error:
            return emit_error(error, exit_code=EXIT_USAGE)
        patch["state"] = text

    if args.ai_notes is not None:
        warning = _note_shrink_warning("aiNotes", existing.ai_notes, args.ai_notes, "append-ai-notes")
        if warning:
            warnings.append(warning)
        patch["aiNotes"] = args.ai_notes
    for addition, source, field, current, label in (
        (args.append_ai_notes, args.append_ai_notes_file, "aiNotes", existing.ai_notes, "append-ai-notes-file"),
        (args.append_human_notes, args.append_human_notes_file, "humanNotes", existing.human_notes, "append-human-notes-file"),
    ):
        if source is not None:
            addition, error = _read_text_input(source, label=label)
            if error:
                return emit_error(error, exit_code=EXIT_USAGE)
            addition = (addition or "").strip()
            if not addition:
                return emit_error(f"--{label} is empty; nothing to append", exit_code=EXIT_USAGE)
        if addition is not None:
            patch[field] = _append_note(current, addition)
    if args.human_notes is not None:
        warning = _note_shrink_warning("humanNotes", existing.human_notes, args.human_notes, "append-human-notes")
        if warning:
            warnings.append(warning)
        patch["humanNotes"] = args.human_notes
    if args.actual_minutes is not None:
        patch["actualMinutes"] = args.actual_minutes
    if args.owner_type is not None or args.owner_name is not None:
        # owner is replaced wholesale; set both flags to preserve type+name.
        patch["owner"] = {
            "type": args.owner_type or "unassigned",
            "name": args.owner_name or "",
        }
    if args.branch is not None or args.worktree is not None:
        patch["workRef"] = git_context.explicit_work_ref(
            branch=args.branch, worktree=args.worktree, workspace=workspace.path,
        )
    if not patch:
        return emit_error(
            "nothing to update: pass a flag like --status/--priority or --patch",
            exit_code=EXIT_USAGE,
        )
    if "workRef" not in patch:
        auto = _auto_work_ref(existing, patch, workspace.path)
        if auto is not None:
            patch["workRef"] = auto

    try:
        task = store.update_task(task_id=existing.id, patch=patch, strict_dependencies=True)
    except TaskNotFoundError as exc:
        return emit_error(str(exc), exit_code=EXIT_NOT_FOUND)
    except DuplicateKeyError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)
    except ValueError as exc:
        return emit_error(_validation_message(str(exc)), exit_code=EXIT_USAGE)

    if "state" in patch and len(task.state or "") > STATE_SOFT_CAP:
        warnings.append(
            f"state is {len(task.state or ''):,} characters (soft cap ~{STATE_SOFT_CAP:,}); keep it to the "
            "verdict, open items and rejected approaches, and move detail to aiNotes."
        )
    if "aiNotes" in patch and "state" not in patch:
        warning = _long_notes_warning(task)
        if warning:
            warnings.append(warning)
    if args.quiet:
        out.emit({"id": task.id, "status": task.status.value}, warnings)
    else:
        out.emit(_present(task.to_dict(), args.full), warnings)
    return EXIT_OK


def _dependents_hint(dependents: list[dict], *, detached: bool) -> str:
    if detached:
        return (
            f"{len(dependents)} remaining task(s) depended on pruned tasks; those satisfied "
            "dependencies were removed so they stay ready."
        )
    return (
        f"{len(dependents)} remaining task(s) depend on deleted tasks and will now be blocked by a "
        "missing dependency; update their dependsOn (see orphanedDependents)."
    )


def _run_delete(args: Namespace, store: TaskStore, out: _Output) -> int:
    if args.ids_file is not None and args.task_id is not None:
        return emit_error("pass a task id/key or --ids-file, not both", exit_code=EXIT_USAGE)
    if args.ids_file is None and args.task_id is None:
        return emit_error("delete needs a task id/key or --ids-file FILE", exit_code=EXIT_USAGE)
    if args.ignore_missing and args.ids_file is None:
        return emit_error("--ignore-missing only applies to --ids-file", exit_code=EXIT_USAGE)

    if args.task_id is not None:
        existing = store.get_task(args.task_id)
        if existing is None:
            out.emit({"deleted": False, "task_id": args.task_id})
            return EXIT_NOT_FOUND
        result = store.delete_tasks([existing.id], dry_run=args.dry_run)
        payload: dict[str, Any] = {"deleted": not args.dry_run, "task_id": existing.id}
        if args.dry_run:
            payload["dryRun"] = True
        if result.dependents:
            payload["orphanedDependents"] = result.dependents
            payload["hint"] = _dependents_hint(result.dependents, detached=False)
        out.emit(payload)
        return EXIT_OK

    refs, error = _read_refs(args.ids_file)
    if error:
        return emit_error(error, exit_code=EXIT_USAGE)
    assert refs is not None
    if not refs:
        return emit_error("--ids-file lists no task ids or keys", exit_code=EXIT_USAGE)
    found, missing = store.resolve_refs(refs)
    if missing and not args.ignore_missing:
        out.emit({
            "deletedCount": 0,
            "matched": len(found),
            "notFound": missing,
            "dryRun": args.dry_run,
            "hint": f"nothing was deleted: {len(missing)} id(s)/key(s) matched no task. "
                    "Fix the file, or pass --ignore-missing to delete the rest.",
        })
        return EXIT_NOT_FOUND
    result = store.delete_tasks([task.id for task in found], dry_run=args.dry_run)
    payload = {
        "deletedCount": 0 if args.dry_run else len(result.deleted),
        "matched": len(result.deleted),
        "tasks": [_ref_label(task) for task in result.deleted],
        "notFound": missing,
        "dryRun": args.dry_run,
    }
    if result.dependents:
        payload["orphanedDependents"] = result.dependents
        payload["hint"] = _dependents_hint(result.dependents, detached=False)
    out.emit(payload)
    return EXIT_OK


def _run_prune(args: Namespace, store: TaskStore, out: _Output, workspace: TodosWorkspace) -> int:
    statuses = args.status or [TaskStatus.COMPLETE.value]
    older_than = None
    if args.older_than is not None:
        older_than = _parse_age(args.older_than)
        if older_than is None:
            return emit_error("--older-than takes an age like 30d, 12h or 2w", exit_code=EXIT_USAGE)
    if args.include_undated and older_than is None:
        return emit_error("--include-undated only applies with --older-than", exit_code=EXIT_USAGE)
    if not args.dry_run and not args.export:
        return emit_error(
            "prune deletes tasks: pass --export FILE to keep a copy first (or --dry-run to preview)",
            exit_code=EXIT_USAGE,
        )

    try:
        selected, undated = store.prune_candidates(
            statuses=statuses, older_than=older_than, include_undated=args.include_undated,
        )
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)

    payload: dict[str, Any] = {
        "prunedCount": 0,
        "matched": len(selected),
        "tasks": [_ref_label(task) for task in selected],
        "statuses": statuses,
        "dryRun": args.dry_run,
    }
    if older_than is not None:
        payload["olderThan"] = args.older_than
    hints: list[str] = []
    if undated:
        payload["skippedUndated"] = len(undated)
        hints.append(
            f"{len(undated)} matching task(s) have no completedAt (closed before dates were recorded); "
            "pass --include-undated to prune them too."
        )

    ids = [task.id for task in selected]
    if args.dry_run or not selected:
        preview = store.delete_tasks(ids, detach_dependents=True, dry_run=True)
        if preview.dependents:
            payload["detachedDependencies"] = preview.dependents
            hints.append(_dependents_hint(preview.dependents, detached=True))
    else:
        export_path = Path(args.export)
        try:
            payload["exportedCount"] = _write_export(export_path, selected, workspace.path)
        except (OSError, ValueError) as exc:
            return emit_error(f"export failed, nothing was deleted: {exc}", exit_code=EXIT_INTERNAL)
        payload["export"] = display_path(export_path.resolve())
        result = store.delete_tasks(ids, detach_dependents=True)
        payload["prunedCount"] = len(result.deleted)
        if result.dependents:
            payload["detachedDependencies"] = result.dependents
            hints.append(_dependents_hint(result.dependents, detached=True))
    if hints:
        payload["hint"] = " ".join(hints)
    out.emit(payload)
    return EXIT_OK
