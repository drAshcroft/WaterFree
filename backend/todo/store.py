"""
Workspace-local todo store backed by `.waterfree/tasks.db`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from backend.session.models import (
    CodeCoord,
    CoordAnchorType,
    DependencyType,
    OwnerType,
    Task,
    TaskDependency,
    TaskOwner,
    TaskPriority,
    TaskStatus,
    TaskTiming,
    TaskType,
)
from backend.todo.dependency_resolver import DependencyResolver
from backend.todo.migration import maybe_import_legacy_json
from backend.todo.utils import (
    TaskStoreData,
    instruction_title,
    json_loads,
    now,
    to_workspace_relative,
)

_PAIRS_DIR = ".waterfree"
_DB_FILE = "tasks.db"
_LEGACY_JSON_FILE = "tasks.json"


class TaskStore:
    def __init__(self, workspace_path: str):
        self._workspace = Path(workspace_path).resolve()
        self._pairs_dir = self._workspace / _PAIRS_DIR
        self._path = self._pairs_dir / _DB_FILE
        self._legacy_json_path = self._pairs_dir / _LEGACY_JSON_FILE

        self._pairs_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        self._deps = DependencyResolver(self._conn)
        maybe_import_legacy_json(
            self._conn, self._legacy_json_path, self.save, self._set_metadata
        )

    @property
    def path(self) -> str:
        return str(self._path)

    # ── Public read API ───────────────────────────────────────────────────────

    def load(self) -> TaskStoreData:
        metadata = self._load_metadata()
        tasks = [
            Task.from_dict(json.loads(row["payload"]))
            for row in self._conn.execute(
                "SELECT payload FROM tasks ORDER BY sort_index ASC, id ASC"
            ).fetchall()
        ]
        return TaskStoreData(
            version=int(metadata.get("version", 1)),
            tasks=tasks,
            phases=json_loads(metadata.get("phases"), []),
            updated_at=str(metadata.get("updated_at") or now()),
            velocity_log=json_loads(metadata.get("velocity_log"), []),
        )

    def list_tasks(
        self,
        *,
        status: str = "",
        owner_name: str = "",
        owner_type: str = "",
        priority: str = "",
        phase: str = "",
        ready_only: bool = False,
        limit: int = 100,
    ) -> TaskStoreData:
        metadata = self._load_metadata()
        tasks = self._query_tasks(
            status=status,
            owner_name=owner_name,
            owner_type=owner_type,
            priority=priority,
            phase=phase,
            ready_only=ready_only,
            limit=limit,
        )
        tasks = tasks[: max(0, limit)]
        return TaskStoreData(
            version=int(metadata.get("version", 1)),
            tasks=tasks,
            phases=json_loads(metadata.get("phases"), []),
            updated_at=str(metadata.get("updated_at") or now()),
            velocity_log=json_loads(metadata.get("velocity_log"), []),
        )

    def search_tasks(self, query: str, limit: int = 20) -> list[Task]:
        if not query.strip():
            return self._query_tasks(limit=limit)

        pattern = f"%{query.casefold()}%"
        rows = self._conn.execute(
            """
            SELECT payload
            FROM tasks
            WHERE lower(title) LIKE ?
               OR lower(description) LIKE ?
               OR lower(rationale) LIKE ?
               OR lower(target_file) LIKE ?
               OR lower(target_class) LIKE ?
               OR lower(target_method) LIKE ?
               OR lower(owner_name) LIKE ?
               OR lower(phase) LIKE ?
               OR lower(json_extract(payload, '$.acceptanceCriteria')) LIKE ?
               OR lower(json_extract(payload, '$.trigger')) LIKE ?
            ORDER BY sort_index ASC, id ASC
            LIMIT ?
            """,
            (pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern, max(0, limit)),
        ).fetchall()
        return [Task.from_dict(json.loads(row["payload"])) for row in rows]

    def get_tasks_by_priority(self, priority: TaskPriority | str) -> list[Task]:
        value = priority.value if isinstance(priority, TaskPriority) else str(priority)
        return self._query_tasks(priority=value)

    def get_tasks_by_phase(self, phase: str) -> list[Task]:
        return self._query_tasks(phase=phase)

    def get_tasks_by_owner(self, owner_name: str) -> list[Task]:
        return self._query_tasks(owner_name=owner_name)

    def get_blocked_tasks(self) -> list[Task]:
        return self._deps.get_blocked_tasks()

    def get_ready_tasks(self) -> list[Task]:
        return self._deps.get_ready_tasks()

    def get_next_task(self, owner_name: str = "", include_unassigned: bool = True) -> Optional[Task]:
        return self._deps.get_next_task(owner_name=owner_name, include_unassigned=include_unassigned)

    # ── Public write API ──────────────────────────────────────────────────────

    def save(self, data: TaskStoreData) -> None:
        data.updated_at = now()
        existing_session_ids = {
            str(row["id"]): row["session_id"]
            for row in self._conn.execute("SELECT id, session_id FROM tasks").fetchall()
        }
        with self._conn:
            self._conn.execute("DELETE FROM task_dependencies")
            self._conn.execute("DELETE FROM tasks")
            for index, task in enumerate(data.tasks):
                self._write_task_row(
                    task,
                    sort_index=index,
                    session_id=existing_session_ids.get(task.id),
                )
                self._replace_dependencies(task)
            self._set_metadata("version", str(data.version))
            self._set_metadata("phases", json.dumps(data.phases, ensure_ascii=True))
            self._set_metadata("updated_at", data.updated_at)
            self._set_metadata("velocity_log", json.dumps(data.velocity_log, ensure_ascii=True))

    def add_task(self, task_input: dict) -> Task:
        data = self.load()
        task = self._task_from_input(task_input)
        data.tasks.append(task)
        if task.phase and task.phase not in data.phases:
            data.phases.append(task.phase)
        self.save(data)
        return task

    def update_task(self, task_id: str, patch: dict) -> Task:
        data = self.load()
        task = next((candidate for candidate in data.tasks if candidate.id == task_id), None)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        self._apply_patch(task, patch)
        if task.status == TaskStatus.COMPLETE and task.timing == TaskTiming.RECURRING:
            task.status = TaskStatus.PENDING
            task.completed_at = None
        if task.phase and task.phase not in data.phases:
            data.phases.append(task.phase)
        self.save(data)
        return task

    def delete_task(self, task_id: str) -> bool:
        data = self.load()
        before = len(data.tasks)
        data.tasks = [task for task in data.tasks if task.id != task_id]
        deleted = len(data.tasks) != before
        if deleted:
            self.save(data)
        return deleted

    def add_phase(self, name: str) -> list[str]:
        data = self.load()
        phase = name.strip()
        if phase and phase not in data.phases:
            data.phases.append(phase)
            self.save(data)
        return data.phases

    def save_task_board(self, layout: list[dict], phases: list[str]) -> TaskStoreData:
        data = self.load()
        tasks_by_id = {task.id: task for task in data.tasks}
        ordered: list[Task] = []
        seen: set[str] = set()

        for item in layout:
            task_id = str(item.get("id", "")).strip()
            if not task_id or task_id in seen:
                continue
            task = tasks_by_id.get(task_id)
            if not task:
                continue

            if "phase" in item:
                task.phase = self._normalize_phase(item.get("phase"))

            ordered.append(task)
            seen.add(task_id)

        for task in data.tasks:
            if task.id not in seen:
                ordered.append(task)

        data.tasks = ordered
        data.phases = self._normalize_phase_list(phases, ordered)
        self.save(data)
        return data

    def promote_to_session(self, task_id: str, session_id: str) -> Task:
        task = self._fetch_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        if task.status == TaskStatus.PENDING:
            task.status = TaskStatus.EXECUTING
        self._upsert_task_record(task, self._sort_index_for_task(task_id), session_id=session_id)
        return task

    def demote_to_backlog(self, task_id: str) -> Task:
        task = self._fetch_task(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        if task.status == TaskStatus.EXECUTING:
            task.status = TaskStatus.PENDING
        self._upsert_task_record(task, self._sort_index_for_task(task_id), session_id=None)
        return task

    def import_from_session(self, session) -> list[Task]:
        imported: list[Task] = []
        for task in getattr(session, "tasks", []):
            if task.status == TaskStatus.COMPLETE:
                continue
            existing = self._fetch_task(task.id)
            session_id = self._session_id_for_task(task.id) if existing else None
            self._upsert_task_record(task, self._sort_index_for_task(task.id, allow_append=True), session_id=session_id)
            imported.append(task)
        self._ensure_phases([task.phase for task in imported if task.phase])
        return imported

    def queue_todo(self, *, file_path: str, line: int, instruction: str) -> Task:
        rel_file = to_workspace_relative(self._workspace, file_path)
        normalized = instruction.strip()
        existing = self._conn.execute(
            """
            SELECT payload
            FROM tasks
            WHERE target_file = ?
              AND target_line = ?
              AND description = ?
              AND status NOT IN (?, ?)
            ORDER BY sort_index ASC, id ASC
            LIMIT 1
            """,
            (
                rel_file,
                line,
                normalized,
                TaskStatus.COMPLETE.value,
                TaskStatus.SKIPPED.value,
            ),
        ).fetchone()
        if existing:
            return Task.from_dict(json.loads(existing["payload"]))

        task = Task(
            title=instruction_title(normalized),
            description=normalized,
            target_coord=CodeCoord(
                file=rel_file,
                line=line,
                anchor_type=CoordAnchorType.MODIFY,
            ),
            priority=TaskPriority.P2,
            owner=TaskOwner(type=OwnerType.UNASSIGNED, name=""),
            task_type=TaskType.IMPL,
            status=TaskStatus.PENDING,
        )
        self._upsert_task_record(task, self._next_sort_index(), session_id=None)
        return task

    def close(self) -> None:
        self._conn.close()

    # ── Input helpers ─────────────────────────────────────────────────────────

    def _task_from_input(self, task_input: dict) -> Task:
        payload = dict(task_input)
        if "targetCoord" in payload:
            coord = CodeCoord.from_dict(payload["targetCoord"])
        else:
            coord = CodeCoord()
        coord.file = to_workspace_relative(self._workspace, coord.file)

        owner_payload = payload.get("owner", {})
        owner = TaskOwner.from_dict(owner_payload) if owner_payload else TaskOwner()
        return Task(
            title=str(payload.get("title", "")).strip(),
            description=str(payload.get("description", "")).strip(),
            rationale=str(payload.get("rationale", "")).strip(),
            target_coord=coord,
            context_coords=[CodeCoord.from_dict(item) for item in payload.get("contextCoords", [])],
            priority=TaskPriority(payload.get("priority", TaskPriority.P2.value)),
            phase=payload.get("phase"),
            depends_on=[TaskDependency.from_dict(item) for item in payload.get("dependsOn", [])],
            blocked_reason=payload.get("blockedReason"),
            owner=owner,
            task_type=TaskType(payload.get("taskType", TaskType.IMPL.value)),
            estimated_minutes=payload.get("estimatedMinutes"),
            actual_minutes=payload.get("actualMinutes"),
            status=TaskStatus(payload.get("status", TaskStatus.PENDING.value)),
            human_notes=payload.get("humanNotes"),
            ai_notes=payload.get("aiNotes"),
            annotations=[],
            started_at=payload.get("startedAt"),
            completed_at=payload.get("completedAt"),
            acceptance_criteria=payload.get("acceptanceCriteria") or None,
            trigger=payload.get("trigger") or None,
            timing=TaskTiming(payload.get("timing", TaskTiming.ONE_TIME.value)),
        )

    def _apply_patch(self, task: Task, patch: dict) -> None:
        if "title" in patch:
            task.title = str(patch["title"]).strip()
        if "description" in patch:
            task.description = str(patch["description"]).strip()
        if "rationale" in patch:
            task.rationale = str(patch["rationale"]).strip()
        if "priority" in patch:
            task.priority = TaskPriority(str(patch["priority"]))
        if "phase" in patch:
            task.phase = str(patch["phase"]).strip() or None
        if "blockedReason" in patch:
            task.blocked_reason = str(patch["blockedReason"]).strip() or None
        if "owner" in patch:
            task.owner = TaskOwner.from_dict(patch["owner"] or {})
        if "taskType" in patch:
            task.task_type = TaskType(str(patch["taskType"]))
        if "estimatedMinutes" in patch:
            task.estimated_minutes = patch["estimatedMinutes"]
        if "actualMinutes" in patch:
            task.actual_minutes = patch["actualMinutes"]
        if "status" in patch:
            task.status = TaskStatus(str(patch["status"]))
        if "humanNotes" in patch:
            task.human_notes = patch["humanNotes"]
        if "aiNotes" in patch:
            task.ai_notes = patch["aiNotes"]
        if "startedAt" in patch:
            task.started_at = patch["startedAt"]
        if "completedAt" in patch:
            task.completed_at = patch["completedAt"]
        if "targetCoord" in patch:
            coord = CodeCoord.from_dict(patch["targetCoord"] or {})
            coord.file = to_workspace_relative(self._workspace, coord.file)
            task.target_coord = coord
        if "dependsOn" in patch:
            task.depends_on = [TaskDependency.from_dict(item) for item in patch["dependsOn"] or []]
        if "contextCoords" in patch:
            task.context_coords = [CodeCoord.from_dict(item) for item in patch["contextCoords"] or []]
        if "acceptanceCriteria" in patch:
            task.acceptance_criteria = patch["acceptanceCriteria"] or None
        if "trigger" in patch:
            task.trigger = patch["trigger"] or None
        if "timing" in patch:
            task.timing = TaskTiming(str(patch["timing"]))

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    sort_index INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    rationale TEXT NOT NULL DEFAULT '',
                    target_file TEXT NOT NULL DEFAULT '',
                    target_class TEXT NOT NULL DEFAULT '',
                    target_method TEXT NOT NULL DEFAULT '',
                    target_line INTEGER,
                    priority TEXT NOT NULL DEFAULT 'P2',
                    phase TEXT NOT NULL DEFAULT '',
                    owner_name TEXT NOT NULL DEFAULT '',
                    owner_type TEXT NOT NULL DEFAULT 'unassigned',
                    status TEXT NOT NULL DEFAULT 'pending',
                    started_at TEXT NOT NULL DEFAULT '',
                    session_id TEXT
                )
                """
            )
            self._ensure_task_columns()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('version', '1')")
            self._conn.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('phases', '[]')")
            self._conn.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('updated_at', ?)", (now(),)
            )
            self._conn.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('velocity_log', '[]')")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task_id TEXT NOT NULL,
                    depends_on_task_id TEXT NOT NULL,
                    dep_type TEXT NOT NULL,
                    PRIMARY KEY (task_id, depends_on_task_id, dep_type)
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_phase ON tasks(phase)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner_name ON tasks(owner_name)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner_type ON tasks(owner_type)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_dependencies_task ON task_dependencies(task_id, dep_type)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_dependencies_target ON task_dependencies(depends_on_task_id, dep_type)"
            )
            self._backfill_task_columns_from_payload()

    def _load_metadata(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM metadata").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def _set_metadata(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            (key, value),
        )

    def _query_tasks(
        self,
        *,
        status: str = "",
        owner_name: str = "",
        owner_type: str = "",
        priority: str = "",
        phase: str = "",
        ready_only: bool = False,
        limit: int = 100,
    ) -> list[Task]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if owner_name:
            clauses.append("lower(owner_name) = ?")
            params.append(owner_name.casefold())
        if owner_type:
            clauses.append("owner_type = ?")
            params.append(owner_type)
        if priority:
            clauses.append("priority = ?")
            params.append(priority)
        if phase:
            clauses.append("phase = ?")
            params.append(phase)
        if ready_only:
            clauses.append(self._deps.ready_condition_sql())
            clauses.append("status NOT IN (?, ?)")
            params.extend([TaskStatus.COMPLETE.value, TaskStatus.SKIPPED.value])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""
            SELECT payload
            FROM tasks
            {where_sql}
            ORDER BY {self._default_order_sql()}
            LIMIT ?
            """,
            [*params, max(0, limit)],
        ).fetchall()
        return [Task.from_dict(json.loads(row["payload"])) for row in rows]

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

    def _ensure_task_columns(self) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        expected = {
            "title": "TEXT NOT NULL DEFAULT ''",
            "description": "TEXT NOT NULL DEFAULT ''",
            "rationale": "TEXT NOT NULL DEFAULT ''",
            "target_file": "TEXT NOT NULL DEFAULT ''",
            "target_class": "TEXT NOT NULL DEFAULT ''",
            "target_method": "TEXT NOT NULL DEFAULT ''",
            "target_line": "INTEGER",
            "priority": "TEXT NOT NULL DEFAULT 'P2'",
            "phase": "TEXT NOT NULL DEFAULT ''",
            "owner_name": "TEXT NOT NULL DEFAULT ''",
            "owner_type": "TEXT NOT NULL DEFAULT 'unassigned'",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "started_at": "TEXT NOT NULL DEFAULT ''",
            "session_id": "TEXT",
        }
        for name, ddl in expected.items():
            if name not in columns:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")

    def _backfill_task_columns_from_payload(self) -> None:
        rows = self._conn.execute(
            """
            SELECT id, sort_index, payload, title, description, rationale, target_file,
                   target_class, target_method, target_line, priority, phase,
                   owner_name, owner_type, status, started_at, session_id
            FROM tasks
            """
        ).fetchall()
        for row in rows:
            task = Task.from_dict(json.loads(row["payload"]))
            self._write_task_row(task, sort_index=int(row["sort_index"]), session_id=row["session_id"])

    def _fetch_task(self, task_id: str) -> Optional[Task]:
        row = self._conn.execute(
            "SELECT payload FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return Task.from_dict(json.loads(row["payload"]))

    def _sort_index_for_task(self, task_id: str, allow_append: bool = False) -> int:
        row = self._conn.execute(
            "SELECT sort_index FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row:
            return int(row["sort_index"])
        if allow_append:
            return self._next_sort_index()
        raise ValueError(f"Task not found: {task_id}")

    def _session_id_for_task(self, task_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT session_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return row["session_id"]

    def _next_sort_index(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(sort_index), -1) AS max_sort FROM tasks").fetchone()
        return int(row["max_sort"]) + 1 if row else 0

    def _normalize_phase(self, value: object) -> Optional[str]:
        phase = str(value or "").strip()
        return phase or None

    def _normalize_phase_list(self, phases: list[str], tasks: list[Task]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        for raw in phases:
            phase = str(raw or "").strip()
            if phase and phase not in seen:
                ordered.append(phase)
                seen.add(phase)

        for task in tasks:
            phase = str(task.phase or "").strip()
            if phase and phase not in seen:
                ordered.append(phase)
                seen.add(phase)

        return ordered

    def _upsert_task_record(self, task: Task, sort_index: int, session_id: Optional[str]) -> None:
        with self._conn:
            self._write_task_row(task, sort_index=sort_index, session_id=session_id)
            self._replace_dependencies(task)
            self._ensure_phases([task.phase] if task.phase else [])
            self._set_metadata("updated_at", now())

    def _write_task_row(self, task: Task, *, sort_index: int, session_id: Optional[str]) -> None:
        payload = json.dumps(task.to_dict(), ensure_ascii=True)
        self._conn.execute(
            """
            INSERT INTO tasks(
                id, sort_index, payload, title, description, rationale, target_file,
                target_class, target_method, target_line, priority, phase,
                owner_name, owner_type, status, started_at, session_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                sort_index = excluded.sort_index,
                payload = excluded.payload,
                title = excluded.title,
                description = excluded.description,
                rationale = excluded.rationale,
                target_file = excluded.target_file,
                target_class = excluded.target_class,
                target_method = excluded.target_method,
                target_line = excluded.target_line,
                priority = excluded.priority,
                phase = excluded.phase,
                owner_name = excluded.owner_name,
                owner_type = excluded.owner_type,
                status = excluded.status,
                started_at = excluded.started_at,
                session_id = excluded.session_id
            """,
            (
                task.id,
                sort_index,
                payload,
                task.title,
                task.description,
                task.rationale,
                task.target_coord.file,
                task.target_coord.class_name or "",
                task.target_coord.method or "",
                task.target_coord.line,
                task.priority.value,
                task.phase or "",
                task.owner.name,
                task.owner.type.value,
                task.status.value,
                task.started_at or "",
                session_id,
            ),
        )

    def _replace_dependencies(self, task: Task) -> None:
        self._conn.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task.id,))
        if not task.depends_on:
            return
        self._conn.executemany(
            "INSERT INTO task_dependencies(task_id, depends_on_task_id, dep_type) VALUES (?, ?, ?)",
            [(task.id, dep.task_id, dep.type.value) for dep in task.depends_on],
        )

    def _ensure_phases(self, phases: list[str]) -> None:
        cleaned = [phase.strip() for phase in phases if phase and phase.strip()]
        if not cleaned:
            return
        metadata = self._load_metadata()
        existing = json_loads(metadata.get("phases"), [])
        changed = False
        for phase in cleaned:
            if phase not in existing:
                existing.append(phase)
                changed = True
        if changed:
            self._set_metadata("phases", json.dumps(existing, ensure_ascii=True))
