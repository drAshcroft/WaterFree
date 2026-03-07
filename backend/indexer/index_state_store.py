"""
SQLite-backed workspace index tracking for quick staleness checks.

Stored at:
  <workspace>/.waterfree/index.db
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".waterfree",
    ".waterfree",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "coverage",
}


@dataclass
class CheckResult:
    has_prior_index: bool
    changed_count: int
    changed_paths: list[str]
    scanned_files: int
    snapshot: dict[str, tuple[int, int]]


class IndexStateStore:
    def __init__(self, workspace_path: str):
        self._workspace_path = Path(workspace_path).resolve()
        self._pairs_dir = self._workspace_path / ".waterfree"
        self._db_path = self._pairs_dir / "index.db"
        self._pairs_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @property
    def db_path(self) -> str:
        return str(self._db_path)

    def quick_check(self) -> CheckResult:
        current = self._scan_workspace()
        previous = self._load_snapshot()

        # Keep this metadata fresh even when no indexing is needed.
        self._set_meta("last_check_at", _utc_now())
        self._set_meta("last_scan_file_count", str(len(current)))

        if not previous:
            return CheckResult(
                has_prior_index=False,
                changed_count=len(current),
                changed_paths=sorted(current.keys())[:25],
                scanned_files=len(current),
                snapshot=current,
            )

        changed: list[str] = []
        old = dict(previous)

        for rel_path, stat_tuple in current.items():
            prev_tuple = old.pop(rel_path, None)
            if prev_tuple != stat_tuple:
                changed.append(rel_path)

        # Remaining entries in old are files that were deleted.
        if old:
            changed.extend(old.keys())

        changed.sort()
        return CheckResult(
            has_prior_index=True,
            changed_count=len(changed),
            changed_paths=changed[:25],
            scanned_files=len(current),
            snapshot=current,
        )

    def record_index(self, check: CheckResult, reason: str, index_result: dict) -> None:
        timestamp = _utc_now()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM files")
            conn.executemany(
                "INSERT INTO files(path, mtime_ns, size_bytes) VALUES (?, ?, ?)",
                (
                    (rel_path, stat_tuple[0], stat_tuple[1])
                    for rel_path, stat_tuple in check.snapshot.items()
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("schema_version", "1"),
                    ("workspace_path", str(self._workspace_path)),
                    ("last_indexed_at", timestamp),
                    ("last_index_reason", reason),
                    ("last_index_changed_count", str(check.changed_count)),
                    ("last_index_file_count", str(check.scanned_files)),
                    ("last_index_result", json.dumps(index_result)),
                ],
            )
            conn.commit()

    def has_any_index_state(self) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT 1 FROM files LIMIT 1").fetchone()
        return row is not None

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    mtime_ns INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _load_snapshot(self) -> dict[str, tuple[int, int]]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("SELECT path, mtime_ns, size_bytes FROM files").fetchall()
        return {row[0]: (int(row[1]), int(row[2])) for row in rows}

    def _set_meta(self, key: str, value: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    def _scan_workspace(self) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}

        for root, dirnames, filenames in os.walk(self._workspace_path, topdown=True):
            # Prune ignored directories in-place for os.walk.
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]

            for name in filenames:
                abs_path = Path(root) / name
                try:
                    stat = abs_path.stat()
                except OSError:
                    continue

                if not abs_path.is_file():
                    continue

                rel_path = abs_path.relative_to(self._workspace_path).as_posix()
                snapshot[rel_path] = (int(stat.st_mtime_ns), int(stat.st_size))

        return snapshot


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
