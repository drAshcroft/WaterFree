"""Usage log for the `waterfree` CLI — one JSONL record per invocation, fail-silent.

Why this exists: the CLI is how agents reach WaterFree, and until now nothing
recorded which areas they actually call, how often a search comes back empty,
or how many bytes of context each call costs. Those numbers decide which
helpers earn their keep. `waterfree usage summary` aggregates this file.

Record shape (one JSON object per line):

    ts            ISO-8601 UTC
    source        "cli" (live) | "transcript" (backfilled by `usage import-transcripts`)
    area, action  e.g. "todos", "search"
    workspace     absolute path the command targeted ("" for global areas)
    argv          the argument list after `<area> <action>`, each value clipped
    query         the positional query for search-like actions, if any
    exit_code     process exit code the dispatcher returned
    duration_ms   wall-clock time inside the action runner
    result_bytes  size of the JSON/text written to stdout
    hits          the envelope's `total` when the action emitted one
    returned      the envelope's `returned` / len(tasks|entries)
    truncated     the envelope's `truncated` flag
    hint          whether the envelope carried a `hint`
    hit_ids       ids of the first few returned tasks / entries (retrieval stats)
    agent         AI_AGENT env (Claude Code sets it), else "unknown"
    session       CLAUDE_CODE_SESSION_ID env when present

Opt-out: WATERFREE_USAGE_LOG=0|off|false. Redirect: WATERFREE_USAGE_LOG=<path>.
The default location is ~/.waterfree/global/usage.jsonl (next to knowledge.db).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ENV = "WATERFREE_USAGE_LOG"
_OFF = ("0", "off", "false", "no")
_MAX_ARG_CHARS = 120
_MAX_HIT_IDS = 10
_QUERY_ACTIONS = ("search", "search-code", "search-graph", "query", "trace", "get-snippet", "get", "ask")
_TRANSCRIPT_FILE = "usage-transcripts.jsonl"


def log_path() -> Path | None:
    """Where live records go, or None when logging is switched off."""
    raw = os.environ.get(_ENV, "").strip()
    if raw.lower() in _OFF:
        return None
    if raw:
        return Path(raw).expanduser()
    return global_dir() / "usage.jsonl"


def transcript_log_path() -> Path:
    """Where `usage import-transcripts` writes its backfill (rewritten wholesale)."""
    live = log_path()
    base = live.parent if live is not None else global_dir()
    return base / _TRANSCRIPT_FILE


def global_dir() -> Path:
    return Path.home() / ".waterfree" / "global"


def agent_identity() -> tuple[str, str]:
    """(agent, session) from the environment Claude Code / Codex export."""
    agent = os.environ.get("AI_AGENT", "").strip()
    if not agent:
        if os.environ.get("CLAUDECODE"):
            agent = "claude-code"
        elif os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_HOME"):
            agent = "codex"
        else:
            agent = "unknown"
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    return agent, session


def _clip(value: object) -> str:
    text = str(value)
    if len(text) <= _MAX_ARG_CHARS:
        return text
    return text[:_MAX_ARG_CHARS] + f"…(+{len(text) - _MAX_ARG_CHARS})"


def sanitize_argv(argv: list[str]) -> list[str]:
    return [_clip(item) for item in argv]


def query_from_argv(action: str, argv: list[str]) -> str:
    """The first positional (non-flag) argument for query-shaped actions."""
    if action not in _QUERY_ACTIONS:
        return ""
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item.startswith("--"):
            skip_next = "=" not in item
            continue
        return _clip(item)
    return ""


def envelope_fields(payload: Any) -> dict[str, Any]:
    """Pull the retrieval-quality fields out of a CLI result envelope."""
    out: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return out
    rows = None
    for key in ("tasks", "entries", "results", "nodes", "symbols", "matches"):
        if isinstance(payload.get(key), list):
            rows = payload[key]
            break
    if isinstance(payload.get("total"), int):
        out["hits"] = payload["total"]
    elif isinstance(payload.get("count"), int):
        out["hits"] = payload["count"]
    elif rows is not None:
        out["hits"] = len(rows)
    if isinstance(payload.get("returned"), int):
        out["returned"] = payload["returned"]
    elif rows is not None:
        out["returned"] = len(rows)
    if "truncated" in payload:
        out["truncated"] = bool(payload["truncated"])
    if payload.get("hint"):
        out["hint"] = True
    if rows:
        ids = [row.get("id") for row in rows[:_MAX_HIT_IDS] if isinstance(row, dict) and row.get("id")]
        if ids:
            out["hit_ids"] = ids
    return out


def build_record(
    *,
    area: str,
    action: str,
    argv: list[str],
    workspace: str,
    exit_code: int,
    duration_ms: float,
    result_bytes: int,
    payload: Any,
    source: str = "cli",
    ts: str | None = None,
    agent: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    env_agent, env_session = agent_identity()
    record: dict[str, Any] = {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "source": source,
        "area": area,
        "action": action,
        "workspace": workspace,
        "argv": sanitize_argv(argv),
        "query": query_from_argv(action, argv),
        "exit_code": exit_code,
        "duration_ms": round(duration_ms, 1),
        "result_bytes": result_bytes,
        "agent": agent if agent is not None else env_agent,
        "session": session if session is not None else env_session,
    }
    record.update(envelope_fields(payload))
    return record


# Extension (JSON-RPC) methods grouped into the same areas the CLI uses, so
# `waterfree usage summary` can compare the two paths. Anything unlisted is
# logged under "server" and still counted.
_METHOD_AREAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("todos", ("listTasks", "getTaskBoard", "searchTasks", "addTask", "updateTask", "deleteTask",
               "saveTaskBoard", "whatNext", "queueTodoInstruction", "promoteWizardTodos")),
    ("knowledge", ("buildKnowledge", "addKnowledgeRepo", "extractProcedure", "searchKnowledge",
                   "listKnowledgeSources", "browseKnowledgeIndex", "removeKnowledgeSource",
                   "addKnowledgeEntry", "deleteKnowledgeEntry")),
    ("index", ("indexWorkspace", "indexStatus", "getGraphSchema", "getArchitecture", "listProjects",
               "deleteProject", "getADR", "storeADR", "updateADR", "deleteADR")),
    ("qa-summary", ("runQaSummary",)),
    ("tutorialize", ("analyzeTutorializeRepo", "generateTutorials", "tutorializeChat")),
)


def area_for_method(method: str) -> str:
    for area, methods in _METHOD_AREAS:
        if method in methods:
            return area
    return "server"


def log_server_call(
    *,
    method: str,
    params: Any,
    result: Any,
    ok: bool,
    duration_ms: float,
) -> None:
    """Record one extension JSON-RPC request in the same shape as a CLI call."""
    try:
        workspace = ""
        if isinstance(params, dict):
            workspace = str(params.get("workspacePath") or params.get("workspace") or "")
        try:
            size = len(json.dumps(result, ensure_ascii=False)) if result is not None else 0
        except (TypeError, ValueError):
            size = 0
        append(build_record(
            area=area_for_method(method),
            action=method,
            argv=[],
            workspace=workspace,
            exit_code=0 if ok else 1,
            duration_ms=duration_ms,
            result_bytes=size,
            payload=result,
            source="extension",
            agent="extension",
        ))
    except Exception:  # pragma: no cover - never disturb the request
        pass


def append(record: dict[str, Any], path: Path | None = None) -> bool:
    """Append one record. Never raises; returns whether it was written."""
    try:
        target = path if path is not None else log_path()
        if target is None:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # pragma: no cover - deliberately swallowed
        try:
            sys.stderr.write(f"note: usage log skipped ({exc})\n")
        except Exception:
            pass
        return False


def read_records(paths: list[Path]) -> list[dict[str, Any]]:
    """Load every well-formed record from the given files (missing files are fine)."""
    records: list[dict[str, Any]] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("area"):
                        records.append(item)
        except OSError:
            continue
    return records
