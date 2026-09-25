"""`waterfree usage ...` — read the CLI usage log and say what the helpers are worth.

`summary` answers: which areas get called, how often a search returns nothing,
how many bytes of context each action costs, which knowledge entries are ever
retrieved. `import-transcripts` backfills the same record shape from Claude
Code's local session transcripts so history from before the log existed counts.
"""

from __future__ import annotations

import json
import re
import shlex
import statistics
from argparse import Namespace, _SubParsersAction
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.cli import usage_log
from backend.cli._common import EXIT_OK, EXIT_USAGE, emit_error, emit_json

_CMD_RE = re.compile(
    r"\bwaterfree(?:\.exe)?\s+(todos|knowledge|index|testing|qa|qa-summary|writing-grade|vision|imagegen|usage)\s+([a-z][a-z0-9-]*)"
)
_TOTAL_RE = re.compile(r'"total":\s*(\d+)')
_ID_RE = re.compile(r'"id":\s*"([0-9a-f]{8}-[0-9a-f-]{27})"')
_SINCE_RE = re.compile(r"^(\d+)([dhw])$")


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("usage", help="Usage log of the waterfree CLI itself")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_summary = actions.add_parser("summary", help="Aggregate the usage log")
    p_summary.add_argument("--since", default="30d",
                           help="Window such as 7d, 12h, 2w, or 'all' (default 30d).")
    # dest is not "area": the dispatcher already uses that name for the CLI area itself.
    p_summary.add_argument("--area", dest="area_filter", default="", help="Only this area (e.g. knowledge).")
    p_summary.add_argument("--workspace", default=None,
                           help="Only calls that targeted this project root.")
    p_summary.add_argument("--source", default="", choices=("", "cli", "extension", "transcript"),
                           help="Only live CLI records, only extension (serve) requests, or only transcript backfill.")
    p_summary.add_argument("--top", type=int, default=15, help="Rows per ranking (default 15).")

    p_tail = actions.add_parser("tail", help="Show the most recent records")
    p_tail.add_argument("-n", "--lines", type=int, default=20)
    p_tail.add_argument("--area", dest="area_filter", default="")
    p_tail.add_argument("--workspace", default=None)

    p_path = actions.add_parser("path", help="Print where the log files live")

    p_import = actions.add_parser(
        "import-transcripts",
        help="Backfill records from Claude Code session transcripts (rewrites the "
             "transcript log wholesale; safe to re-run).",
    )
    p_import.add_argument("--claude-projects", default=None,
                          help="Directory of Claude Code project transcripts "
                               "(default ~/.claude/projects).")

    p.set_defaults(_runner=run)


# ── helpers ────────────────────────────────────────────────────────────────

def _parse_since(raw: str) -> datetime | None:
    text = (raw or "").strip().lower()
    if text in ("", "all", "0"):
        return None
    match = _SINCE_RE.match(text)
    if not match:
        raise ValueError(f"--since must look like 7d, 12h, 2w or 'all' (got {raw!r})")
    amount, unit = int(match.group(1)), match.group(2)
    delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
    return datetime.now(timezone.utc) - delta


def _record_time(record: dict) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(record.get("ts", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _norm_workspace(path: str | None) -> str:
    if not path:
        return ""
    return str(Path(path).resolve()).replace("\\", "/").rstrip("/").lower()


def _filter(records: Iterable[dict], *, since: datetime | None, area: str,
            workspace: str | None, source: str) -> list[dict]:
    ws = _norm_workspace(workspace)
    out: list[dict] = []
    for record in records:
        if area and record.get("area") != area:
            continue
        if source and record.get("source", "cli") != source:
            continue
        if ws and _norm_workspace(record.get("workspace")) != ws:
            continue
        if since is not None:
            stamp = _record_time(record)
            if stamp is None or stamp < since:
                continue
        out.append(record)
    return out


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _p90(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]


def summarize(records: list[dict], *, top: int = 15) -> dict[str, Any]:
    """Aggregate records into the numbers that say whether a helper is used and useful."""
    by_action: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_action[f"{record.get('area')} {record.get('action')}"].append(record)

    actions: list[dict[str, Any]] = []
    for key, rows in sorted(by_action.items(), key=lambda kv: -len(kv[1])):
        hits = [r["hits"] for r in rows if isinstance(r.get("hits"), int)]
        sizes = [r["result_bytes"] for r in rows if isinstance(r.get("result_bytes"), int)]
        durations = [r["duration_ms"] for r in rows if isinstance(r.get("duration_ms"), (int, float))]
        errors = sum(1 for r in rows if r.get("exit_code") not in (0, None))
        row: dict[str, Any] = {
            "action": key,
            "calls": len(rows),
            "sessions": len({r.get("session") for r in rows if r.get("session")}),
            "workspaces": len({_norm_workspace(r.get("workspace")) for r in rows if r.get("workspace")}),
            "error_rate": round(errors / len(rows), 2),
            "median_result_bytes": _median(sizes),
            "p90_result_bytes": _p90(sizes),
            "total_result_bytes": sum(sizes),
        }
        if hits:
            row["searches_with_hits_known"] = len(hits)
            row["zero_hit_rate"] = round(sum(1 for h in hits if h == 0) / len(hits), 2)
            row["median_hits"] = _median(hits)
        if durations:
            row["median_duration_ms"] = _median(durations)
        actions.append(row)

    by_area = Counter(str(r.get("area")) for r in records)
    by_agent = Counter(str(r.get("agent") or "unknown") for r in records)
    by_workspace = Counter(_norm_workspace(r.get("workspace")) or "(global)" for r in records)
    by_day = Counter(str(r.get("ts", ""))[:10] for r in records)

    retrieved: Counter = Counter()
    for record in records:
        if record.get("area") == "knowledge" and record.get("action") in ("search", "browse", "get", "inject"):
            for entry_id in record.get("hit_ids") or []:
                retrieved[entry_id] += 1
    knowledge: dict[str, Any] = {
        "searches": sum(1 for r in records if r.get("area") == "knowledge" and r.get("action") == "search"),
        "adds": sum(1 for r in records if r.get("area") == "knowledge" and r.get("action") == "add"),
        "distinct_entries_retrieved": len(retrieved),
        "top_retrieved_entries": retrieved.most_common(top),
    }
    if knowledge["searches"]:
        knowledge["adds_per_search"] = round(knowledge["adds"] / knowledge["searches"], 2)

    queries = Counter(
        str(r.get("query")).strip().lower()
        for r in records if r.get("query") and r.get("action") in ("search", "search-code", "search-graph")
    )
    empty_queries = Counter(
        str(r.get("query")).strip().lower()
        for r in records if r.get("query") and r.get("hits") == 0
    )

    stamps = [s for s in (_record_time(r) for r in records) if s]
    return {
        "records": len(records),
        "first": min(stamps).isoformat() if stamps else None,
        "last": max(stamps).isoformat() if stamps else None,
        "sessions": len({r.get("session") for r in records if r.get("session")}),
        "by_source": Counter(str(r.get("source", "cli")) for r in records).most_common(),
        "by_area": by_area.most_common(),
        "by_agent": by_agent.most_common(),
        "by_workspace": by_workspace.most_common(top),
        "by_day": sorted(by_day.items()),
        "actions": actions,
        "knowledge": knowledge,
        "top_queries": queries.most_common(top),
        "top_empty_queries": empty_queries.most_common(top),
    }


# ── transcript backfill ────────────────────────────────────────────────────

def _project_dir_to_workspace(name: str) -> str:
    """Claude Code encodes C:\\Projects\\dungeon as c--Projects-dungeon."""
    if "--" in name:
        drive, rest = name.split("--", 1)
        return f"{drive.upper()}:/{rest.replace('-', '/')}"
    return name.replace("-", "/")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def _argv_after(cmd: str, area: str, action: str) -> tuple[list[str], str]:
    """Split the shell text after `waterfree <area> <action>` and pull out --workspace."""
    marker = re.search(rf"\bwaterfree(?:\.exe)?\s+{re.escape(area)}\s+{re.escape(action)}\b", cmd)
    tail = cmd[marker.end():] if marker else ""
    tail = re.split(r"\s(?:\||;|&&|2>|>|\n)", tail, maxsplit=1)[0]
    # Non-POSIX splitting keeps Windows backslashes intact; quotes are then
    # stripped by hand so both `"a b"` and `'a b'` become one clean token.
    try:
        raw = shlex.split(tail, posix=False)
    except ValueError:
        raw = tail.split()
    argv = [
        item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'" else item
        for item in raw
    ]
    workspace = ""
    for index, item in enumerate(argv):
        if item == "--workspace" and index + 1 < len(argv):
            workspace = argv[index + 1]
        elif item.startswith("--workspace="):
            workspace = item.split("=", 1)[1]
    return argv, workspace


def import_transcripts(projects_dir: Path) -> tuple[list[dict], dict[str, int]]:
    """Pair every `waterfree` tool call in the transcripts with its result."""
    records: list[dict] = []
    stats = {"files": 0, "calls": 0, "unpaired": 0}
    for path in sorted(projects_dir.glob("*/*.jsonl")):
        stats["files"] += 1
        project_ws = _project_dir_to_workspace(path.parent.name)
        session = path.stem
        pending: dict[str, dict] = {}
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "waterfree" not in line and "tool_result" not in line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    content = (item.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    ts = str(item.get("timestamp") or "")
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "tool_use" and part.get("name") in ("Bash", "PowerShell"):
                            cmd = str((part.get("input") or {}).get("command", ""))
                            for area, action in _CMD_RE.findall(cmd):
                                argv, ws = _argv_after(cmd, area, action)
                                pending[str(part.get("id"))] = {
                                    "ts": ts, "area": area, "action": action,
                                    "argv": argv, "workspace": ws or project_ws,
                                }
                                stats["calls"] += 1
                        elif part.get("type") == "tool_result":
                            call = pending.pop(str(part.get("tool_use_id")), None)
                            if call is None:
                                continue
                            text = _text_of(part.get("content"))
                            total = _TOTAL_RE.search(text)
                            payload: dict[str, Any] = {}
                            if total:
                                payload["total"] = int(total.group(1))
                            ids = _ID_RE.findall(text)
                            if ids:
                                payload["entries"] = [{"id": i} for i in ids[:10]]
                            failed = bool(part.get("is_error"))
                            records.append(usage_log.build_record(
                                area=call["area"], action=call["action"], argv=call["argv"],
                                workspace=call["workspace"], exit_code=1 if failed else 0,
                                duration_ms=0.0, result_bytes=len(text), payload=payload,
                                source="transcript", ts=call["ts"] or ts,
                                agent="claude-code", session=session,
                            ))
        except OSError:
            continue
        stats["unpaired"] += len(pending)
    return records, stats


# ── runner ─────────────────────────────────────────────────────────────────

def _all_paths() -> list[Path]:
    live = usage_log.log_path()
    paths = [usage_log.transcript_log_path()]
    if live is not None:
        paths.insert(0, live)
    return paths


def run(args: Namespace) -> int:
    action = args.action

    if action == "path":
        live = usage_log.log_path()
        emit_json({
            "live": str(live) if live else None,
            "transcripts": str(usage_log.transcript_log_path()),
            "enabled": live is not None,
        })
        return EXIT_OK

    if action == "summary":
        try:
            since = _parse_since(args.since)
        except ValueError as exc:
            return emit_error(str(exc), exit_code=EXIT_USAGE)
        records = _filter(
            usage_log.read_records(_all_paths()),
            since=since, area=args.area_filter, workspace=args.workspace, source=args.source,
        )
        summary = summarize(records, top=max(1, args.top))
        summary["window"] = args.since
        summary["files"] = [str(p) for p in _all_paths()]
        emit_json(summary)
        return EXIT_OK

    if action == "tail":
        records = _filter(
            usage_log.read_records(_all_paths()),
            since=None, area=args.area_filter, workspace=args.workspace, source="",
        )
        records.sort(key=lambda r: str(r.get("ts", "")))
        emit_json({"records": records[-max(0, args.lines):]})
        return EXIT_OK

    if action == "import-transcripts":
        projects_dir = Path(args.claude_projects).expanduser() if args.claude_projects else Path.home() / ".claude" / "projects"
        if not projects_dir.is_dir():
            return emit_error(f"no transcript directory at {projects_dir}", exit_code=EXIT_USAGE)
        records, stats = import_transcripts(projects_dir)
        target = usage_log.transcript_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        emit_json({"written": len(records), "path": str(target), **stats})
        return EXIT_OK

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)
