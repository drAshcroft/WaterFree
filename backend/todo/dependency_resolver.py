"""
Dependency-aware task queries — ready tasks, blocked tasks, next task selection.

Operates on the shared SQLite connection owned by TaskStore.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from backend.session.models import OwnerType, Task, TaskStatus


class DependencyResolver:
    """Queries tasks taking their dependency graph into account."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_blocked_tasks(self) -> list[Task]:
        rows = self._conn.execute(
            f"""
            SELECT payload
            FROM tasks
            WHERE {self._blocked_condition_sql()}
              AND status NOT IN (?, ?)
            ORDER BY {self._default_order_sql()}
            """,
            (TaskStatus.COMPLETE.value, TaskStatus.SKIPPED.value),
        ).fetchall()
        return [Task.from_dict(json.loads(row["payload"])) for row in rows]

    def get_ready_tasks(self, limit: int = 100) -> list[Task]:
        rows = self._conn.execute(
            f"""
            SELECT payload
            FROM tasks
            WHERE {self._ready_condition_sql()}
              AND status NOT IN (?, ?)
            ORDER BY {self._default_order_sql()}
            LIMIT ?
            """,
            (TaskStatus.COMPLETE.value, TaskStatus.SKIPPED.value, max(0, limit)),
        ).fetchall()
        return [Task.from_dict(json.loads(row["payload"])) for row in rows]

    def get_next_task(self, owner_name: str = "", include_unassigned: bool = True) -> Optional[Task]:
        clauses = [
            self._ready_condition_sql(),
            "status NOT IN (?, ?)",
        ]
        params: list[object] = [TaskStatus.COMPLETE.value, TaskStatus.SKIPPED.value]

        if owner_name:
            if include_unassigned:
                clauses.append("(lower(owner_name) = ? OR owner_type = ?)")
                params.extend([owner_name.casefold(), OwnerType.UNASSIGNED.value])
            else:
                clauses.append("lower(owner_name) = ?")
                params.append(owner_name.casefold())

        rows = self._conn.execute(
            f"""
            SELECT payload
            FROM tasks
            WHERE {' AND '.join(clauses)}
            ORDER BY {self._default_order_sql()}
            LIMIT 1
            """,
            params,
        ).fetchall()
        if not rows:
            return None
        return Task.from_dict(json.loads(rows[0]["payload"]))

    def ready_condition_sql(self) -> str:
        return self._ready_condition_sql()

    def blocked_condition_sql(self) -> str:
        return self._blocked_condition_sql()

    # ── SQL helpers ───────────────────────────────────────────────────────────

    def _ready_condition_sql(self) -> str:
        return f"NOT EXISTS ({self._blocker_subquery_sql()})"

    def _blocked_condition_sql(self) -> str:
        return f"EXISTS ({self._blocker_subquery_sql()})"

    def _blocker_subquery_sql(self) -> str:
        return """
            SELECT 1
            FROM task_dependencies deps
            LEFT JOIN tasks blocker ON blocker.id = deps.depends_on_task_id
            WHERE deps.task_id = tasks.id
              AND deps.dep_type = 'blocks'
              AND (blocker.id IS NULL OR blocker.status != 'complete')
        """

    def _default_order_sql(self) -> str:
        return """
            CASE priority
                WHEN 'P0' THEN 0
                WHEN 'P1' THEN 1
                WHEN 'P2' THEN 2
                WHEN 'P3' THEN 3
                WHEN 'spike' THEN 4
                ELSE 99
            END ASC,
            CASE WHEN started_at != '' THEN 0 ELSE 1 END ASC,
            sort_index ASC,
            id ASC
        """
