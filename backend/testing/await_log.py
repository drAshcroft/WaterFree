"""
`waterfree testing await`: block until a log shows a line, then report only
the summary lines.

Why this exists: agents waited on long headless runs with sleep/until loops
capped by `timeout 595`, the agent harness blocks plain `sleep N`, and a
Monitor loop fired early because it matched a stale log that an old process
was still holding (KB 4868b5d9, 5f7f0b2b). Each long run cost dozens of polling
tool calls and, at the end, a 300 KB log read into context.

This is one blocking call with its own timeout that:

* ignores everything already in the log when it starts — it tracks the size
  at start and only reads what is appended, and notices a log that was
  truncated or recreated (its first bytes or its file identity change) and
  then reads the new file from the top. `fresh=True` goes further and ignores
  appends to the *existing* file entirely, for when an old process may still
  be writing to it;
* returns as soon as a new line matches `until`, or the watched pid exits, or
  the timeout hits;
* returns only lines matching the summary pattern (PASS/FAIL/SUMMARY/
  Parse Error/... by default), never the whole log.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.testing.bounded import pid_alive

DEFAULT_SUMMARY_PATTERN = (
    r"\b(?:PASS|PASSED|FAIL|FAILED|FAILURES?|SUMMARY)\b|\bSummary\b"
    r"|Parse Error|SCRIPT ERROR|RUNNER FAILED"
    r"|\b\d+ (?:passed|failed|failures?|errors?)\b"
)

DEFAULT_MAX_LINES = 60
DEFAULT_POLL_SECONDS = 0.25

# Bytes compared to tell "appended to" from "rewritten". An append-only log
# never changes its first bytes; a truncate-and-rewrite (Godot's --log-file,
# shell `>` redirection) almost always does.
_HEAD_BYTES = 512


@dataclass
class AwaitOutcome:
    matched: bool
    reason: str  # matched | pid_exited | timeout
    elapsed: float
    log: str
    match_line: str | None = None
    summary_lines: list[str] = field(default_factory=list)
    summary_line_count: int = 0
    truncated: bool = False
    bytes_read: int = 0
    log_exists: bool = False
    log_recreated: bool = False
    pid: int | None = None

    def to_dict(self) -> dict:
        payload = {
            "matched": self.matched,
            "reason": self.reason,
            "elapsedSeconds": round(self.elapsed, 2),
            "log": self.log,
            "matchLine": self.match_line,
            "summaryLines": self.summary_lines,
            "summaryLineCount": self.summary_line_count,
            "bytesRead": self.bytes_read,
            "logExists": self.log_exists,
        }
        if self.truncated:
            payload["truncated"] = True
        if self.log_recreated:
            payload["logRecreated"] = True
        if self.pid is not None:
            payload["pid"] = self.pid
        return payload


class _SummaryCollector:
    """Keeps the first few and the last many summary lines, within a cap.

    The first lines matter (the first Parse Error is usually the cause); the
    last lines carry the verdict. The middle of a 10,000-line sweep does not.
    """

    def __init__(self, pattern: re.Pattern[str], max_lines: int) -> None:
        self.pattern = pattern
        self.max_lines = max(1, max_lines)
        self.head_cap = max(1, self.max_lines // 6)
        self.head: list[str] = []
        self.tail: list[str] = []
        self.count = 0

    def offer(self, line: str) -> None:
        if not self.pattern.search(line):
            return
        self.count += 1
        if len(self.head) < self.head_cap:
            self.head.append(line)
            return
        self.tail.append(line)
        if len(self.tail) > self.max_lines - self.head_cap:
            del self.tail[0]

    def lines(self) -> list[str]:
        return self.head + self.tail

    @property
    def truncated(self) -> bool:
        return self.count > len(self.head) + len(self.tail)


def _identity(stat: os.stat_result) -> tuple:
    return (stat.st_ino, getattr(stat, "st_birthtime", None))


def _read_head(path: Path, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(min(size, _HEAD_BYTES))
    except OSError:
        return b""


def await_log(
    log: str,
    until: str,
    *,
    timeout: float,
    pid: int | None = None,
    summary_pattern: str | None = None,
    max_lines: int = DEFAULT_MAX_LINES,
    fresh: bool = False,
    include_existing: bool = False,
    poll_interval: float = DEFAULT_POLL_SECONDS,
) -> AwaitOutcome:
    """Block until a new log line matches `until`, `pid` exits, or `timeout`.

    Raises ValueError for an invalid regex or contradictory options.
    """
    if fresh and include_existing:
        raise ValueError("--fresh and --include-existing contradict each other")
    try:
        until_re = re.compile(until)
    except re.error as exc:
        raise ValueError(f"--until is not a valid regular expression: {exc}") from exc
    try:
        summary_re = re.compile(summary_pattern or DEFAULT_SUMMARY_PATTERN)
    except re.error as exc:
        raise ValueError(f"--summary-pattern is not a valid regular expression: {exc}") from exc

    path = Path(log)
    started = time.monotonic()
    deadline = started + timeout
    collector = _SummaryCollector(summary_re, max_lines)
    outcome = AwaitOutcome(matched=False, reason="timeout", elapsed=0.0, log=str(path), pid=pid)

    # Where reading starts, and what the file looked like when we arrived.
    offset = 0
    identity: tuple | None = None
    head = b""
    # In fresh mode the pre-existing file is not trusted at all, even its
    # appends: only a recreated (or newly created) log counts.
    waiting_for_new_file = False
    try:
        stat = path.stat()
    except OSError:
        stat = None
    if stat is not None:
        identity = _identity(stat)
        head = _read_head(path, stat.st_size)
        if not include_existing:
            offset = stat.st_size
        waiting_for_new_file = fresh

    partial = b""

    def consume(data: bytes, *, final: bool) -> bool:
        nonlocal partial
        pieces = (partial + data).split(b"\n")
        partial = b"" if final else pieces.pop()
        for raw in pieces:
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if not line and final:
                continue
            collector.offer(line)
            if outcome.match_line is None and until_re.search(line):
                outcome.match_line = line
                return True
        return False

    def poll(*, final: bool = False) -> bool:
        """Read whatever is new; True once `until` has matched."""
        nonlocal offset, identity, head, partial, waiting_for_new_file
        try:
            stat = path.stat()
        except OSError:
            return False
        outcome.log_exists = True
        current = _identity(stat)
        rewritten = False
        if identity is None:
            rewritten = True  # appeared after we started: all of it is new
        elif current != identity or stat.st_size < offset:
            rewritten = True
        elif head:
            now_head = _read_head(path, len(head))
            rewritten = len(now_head) == len(head) and now_head != head
        if rewritten:
            if identity is not None:
                outcome.log_recreated = True
            identity = current
            head = _read_head(path, stat.st_size)
            offset = 0
            partial = b""
            waiting_for_new_file = False
        elif len(head) < _HEAD_BYTES and stat.st_size > len(head):
            head = _read_head(path, stat.st_size)
        if waiting_for_new_file:
            offset = stat.st_size
            return False
        if stat.st_size <= offset and not (final and partial):
            return False
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(max(0, stat.st_size - offset))
        except OSError:
            return False
        offset += len(data)
        outcome.bytes_read += len(data)
        return consume(data, final=final)

    while True:
        if poll():
            outcome.matched, outcome.reason = True, "matched"
            break
        if pid is not None and not pid_alive(pid):
            # One last read: the process may have flushed its verdict on exit.
            if poll(final=True):
                outcome.matched, outcome.reason = True, "matched"
            else:
                outcome.reason = "pid_exited"
            break
        if time.monotonic() >= deadline:
            if poll(final=True):
                outcome.matched, outcome.reason = True, "matched"
            else:
                outcome.reason = "timeout"
            break
        time.sleep(max(0.01, min(poll_interval, deadline - time.monotonic())))

    outcome.elapsed = time.monotonic() - started
    outcome.summary_lines = collector.lines()
    outcome.summary_line_count = collector.count
    outcome.truncated = collector.truncated
    return outcome
