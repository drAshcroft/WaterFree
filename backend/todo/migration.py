"""
Legacy data migration — one-time import from tasks.json to tasks.db.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable

from backend.todo.utils import TaskStoreData, now


def maybe_import_legacy_json(
    conn: sqlite3.Connection,
    legacy_json_path: Path,
    save_fn: Callable[[TaskStoreData], None],
    set_metadata_fn: Callable[[str, str], None],
) -> None:
    """Import tasks from the old JSON file if the DB is empty and the file exists."""
    row = conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()
    task_count = int(row["count"]) if row else 0
    if task_count > 0 or not legacy_json_path.exists():
        return

    payload = json.loads(legacy_json_path.read_text(encoding="utf-8"))
    data = TaskStoreData.from_dict(payload)
    save_fn(data)
    with conn:
        set_metadata_fn("legacy_imported_from", str(legacy_json_path))
        set_metadata_fn("legacy_imported_at", now())
