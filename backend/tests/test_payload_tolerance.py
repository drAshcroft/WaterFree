"""
A foreign writer's payload must not be able to brick a workspace.

The motivating incident: an agent wrote 39 tasks into
`Voxel_Build/.waterfree/tasks.db` directly. 16 carried Claude Code's `TodoWrite`
vocabulary (`"completed"` where `TaskStatus` says `"complete"`) and one carried a
`dependsOn` entry that was a bare key string rather than an object. Because
`Task.from_dict` runs inside `TaskStore.__init__`, either one alone took down the
board, the CLI and `todos validate` together, leaving no way to repair the store
from inside the product.

So the read path tolerates both wrong *values* and wrong *shapes*, records what
it repaired, and lets the store's one-shot backfill write the canonical form
back. Writes stay strict about values they have never heard of.
"""

import json
import sqlite3
import unittest
from pathlib import Path

from backend.session.coord_models import CoordAnchorType
from backend.session.enum_coercion import coerce_enum, parse_enum
from backend.session.models import (
    OwnerType,
    Task,
    TaskPriority,
    TaskStatus,
    TaskTiming,
    TaskType,
)
from backend.test_support import make_temp_dir as make_test_dir
from backend.todo.store import TaskStore


class CoerceEnumTests(unittest.TestCase):
    def test_canonical_value_passes_through_without_warning(self) -> None:
        warnings: list[str] = []
        result = coerce_enum(TaskStatus, "complete", TaskStatus.PENDING, field="status", warnings=warnings)
        self.assertIs(result, TaskStatus.COMPLETE)
        self.assertEqual(warnings, [])

    def test_known_alias_normalizes_and_warns(self) -> None:
        warnings: list[str] = []
        result = coerce_enum(TaskStatus, "completed", TaskStatus.PENDING, field="status", warnings=warnings)
        self.assertIs(result, TaskStatus.COMPLETE)
        self.assertEqual(len(warnings), 1)
        self.assertIn("completed", warnings[0])
        self.assertIn("complete", warnings[0])

    def test_todowrite_vocabulary_maps_across_the_board(self) -> None:
        for spelling, expected in [
            ("completed", TaskStatus.COMPLETE),
            ("in_progress", TaskStatus.EXECUTING),
            ("pending", TaskStatus.PENDING),
        ]:
            with self.subTest(spelling=spelling):
                self.assertIs(
                    coerce_enum(TaskStatus, spelling, TaskStatus.PENDING, field="status"),
                    expected,
                )

    def test_case_and_whitespace_are_not_aliases(self) -> None:
        self.assertIs(
            coerce_enum(TaskPriority, "  p0 ", TaskPriority.P2, field="priority"),
            TaskPriority.P0,
        )

    def test_unknown_value_falls_back_to_default_and_warns(self) -> None:
        warnings: list[str] = []
        result = coerce_enum(TaskStatus, "quantum", TaskStatus.PENDING, field="status", warnings=warnings)
        self.assertIs(result, TaskStatus.PENDING)
        self.assertEqual(len(warnings), 1)
        self.assertIn("quantum", warnings[0])
        self.assertIn("unknown value", warnings[0])

    def test_missing_and_empty_use_the_default_silently(self) -> None:
        warnings: list[str] = []
        self.assertIs(coerce_enum(TaskType, None, TaskType.IMPL, field="taskType", warnings=warnings), TaskType.IMPL)
        self.assertIs(coerce_enum(TaskType, "  ", TaskType.IMPL, field="taskType", warnings=warnings), TaskType.IMPL)
        self.assertEqual(warnings, [])

    def test_non_string_junk_never_raises(self) -> None:
        warnings: list[str] = []
        result = coerce_enum(TaskTiming, {"nope": 1}, TaskTiming.ONE_TIME, field="timing", warnings=warnings)
        self.assertIs(result, TaskTiming.ONE_TIME)
        self.assertEqual(len(warnings), 1)


class ParseEnumTests(unittest.TestCase):
    def test_accepts_the_same_aliases_as_the_reader(self) -> None:
        self.assertIs(parse_enum(TaskStatus, "completed", field="status"), TaskStatus.COMPLETE)
        self.assertIs(parse_enum(OwnerType, "ai", field="owner.type"), OwnerType.AGENT)

    def test_rejects_unknown_values_with_the_valid_list(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            parse_enum(TaskStatus, "quantum", field="status")
        message = str(ctx.exception)
        self.assertIn("invalid status", message)
        self.assertIn("quantum", message)
        self.assertIn("complete", message)


class TaskFromDictToleranceTests(unittest.TestCase):
    def test_foreign_status_reads_as_complete_and_is_recorded(self) -> None:
        task = Task.from_dict({"title": "T", "description": "d", "status": "completed"})
        self.assertIs(task.status, TaskStatus.COMPLETE)
        self.assertEqual(len(task.parse_warnings), 1)

    def test_every_enum_field_survives_garbage_at_once(self) -> None:
        task = Task.from_dict({
            "title": "T",
            "description": "d",
            "status": "???",
            "priority": "???",
            "taskType": "???",
            "timing": "???",
            "owner": {"type": "???", "name": "Olive"},
            "dependsOn": [{"taskId": "x", "type": "???"}],
            "targetCoord": {"file": "a.py", "anchorType": "???"},
            "annotations": [{"summary": "s", "status": "???"}],
        })
        self.assertIs(task.status, TaskStatus.PENDING)
        self.assertIs(task.priority, TaskPriority.P2)
        self.assertIs(task.task_type, TaskType.IMPL)
        self.assertIs(task.timing, TaskTiming.ONE_TIME)
        self.assertIs(task.owner.type, OwnerType.UNASSIGNED)
        self.assertIs(task.target_coord.anchor_type, CoordAnchorType.MODIFY)
        self.assertEqual(task.owner.name, "Olive")
        self.assertEqual(len(task.parse_warnings), 8)

    def test_parse_warnings_are_not_persisted(self) -> None:
        task = Task.from_dict({"title": "T", "description": "d", "status": "completed"})
        self.assertNotIn("parseWarnings", task.to_dict())
        self.assertEqual(Task.from_dict(task.to_dict()).parse_warnings, [])


class PayloadShapeToleranceTests(unittest.TestCase):
    def test_bare_string_dependency_is_kept_as_a_reference(self) -> None:
        task = Task.from_dict({
            "title": "T", "description": "d",
            "dependsOn": ["BAL-012"],
        })
        self.assertEqual([dep.task_id for dep in task.depends_on], ["BAL-012"])
        self.assertEqual(len(task.parse_warnings), 1)

    def test_unusable_dependency_entries_are_dropped_not_fatal(self) -> None:
        task = Task.from_dict({
            "title": "T", "description": "d",
            "dependsOn": [{"taskId": "keep"}, 42, None, ["nested"]],
        })
        self.assertEqual([dep.task_id for dep in task.depends_on], ["keep"])
        self.assertEqual(len(task.parse_warnings), 3)

    def test_scalar_where_a_list_belongs_is_ignored(self) -> None:
        task = Task.from_dict({
            "title": "T", "description": "d",
            "dependsOn": "BAL-012",
            "contextCoords": "src/app.py",
            "annotations": 7,
        })
        self.assertEqual(task.depends_on, [])
        self.assertEqual(task.context_coords, [])
        self.assertEqual(task.annotations, [])
        self.assertEqual(len(task.parse_warnings), 3)

    def test_scalar_where_an_object_belongs_falls_back_to_empty(self) -> None:
        task = Task.from_dict({
            "title": "T", "description": "d",
            "targetCoord": "src/app.py",
            "owner": "Olive",
        })
        self.assertEqual(task.target_coord.file, "")
        self.assertIs(task.owner.type, OwnerType.UNASSIGNED)
        self.assertEqual(len(task.parse_warnings), 2)


class ForeignStatusStoreTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="enum-coercion-")

    def _write_foreign_status(self, workspace: Path, task_id: str, status: str) -> None:
        """Write a status the store would never produce, the way an outside
        writer would: straight into the row, bypassing every validating path."""
        conn = sqlite3.connect(workspace / ".waterfree" / "tasks.db")
        try:
            row = conn.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,)).fetchone()
            payload = json.loads(row[0])
            payload["status"] = status
            conn.execute(
                "UPDATE tasks SET payload = ?, status = ? WHERE id = ?",
                (json.dumps(payload), status, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_store_still_opens_and_normalizes_a_foreign_status(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        task = store.add_task({"title": "Closed by an agent", "description": "d"})
        store._conn.close()

        self._write_foreign_status(workspace, task.id, "completed")

        # The regression: this constructor used to raise
        # "ValueError: 'completed' is not a valid TaskStatus".
        reopened = TaskStore(str(workspace))
        loaded = reopened.load().tasks[0]
        self.assertIs(loaded.status, TaskStatus.COMPLETE)

    def test_validate_reports_the_coercion_as_a_warning_not_an_error(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        task = store.add_task({"title": "Closed by an agent", "description": "d"})
        store._conn.close()

        self._write_foreign_status(workspace, task.id, "completed")

        result = TaskStore(str(workspace)).validate()
        coerced = [issue for issue in result.issues if issue.code == "coerced_value"]
        self.assertEqual(len(coerced), 1)
        self.assertIn("completed", coerced[0].message)
        self.assertEqual(coerced[0].severity, "warning")

    def test_backfill_resolves_a_key_shaped_dependency_and_rebuilds_the_edge_table(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        blocker = store.add_task({"title": "Blocker", "description": "d", "key": "BAL-012"})
        blocked = store.add_task({"title": "Blocked", "description": "d", "key": "ELF-001"})
        store._conn.close()

        # An outside writer names the dependency by its human-facing key, as a
        # bare string, and never touches the denormalized edge table.
        conn = sqlite3.connect(workspace / ".waterfree" / "tasks.db")
        row = conn.execute("SELECT payload FROM tasks WHERE id = ?", (blocked.id,)).fetchone()
        payload = json.loads(row[0])
        payload["dependsOn"] = ["BAL-012"]
        conn.execute("UPDATE tasks SET payload = ? WHERE id = ?", (json.dumps(payload), blocked.id))
        conn.execute("DELETE FROM metadata WHERE key = 'column_schema_version'")
        conn.commit()
        conn.close()

        reopened = TaskStore(str(workspace))
        loaded = next(t for t in reopened.load().tasks if t.key == "ELF-001")
        self.assertEqual([dep.task_id for dep in loaded.depends_on], [blocker.id])

        edges = reopened._conn.execute(
            "SELECT task_id, depends_on_task_id FROM task_dependencies"
        ).fetchall()
        self.assertEqual([(r["task_id"], r["depends_on_task_id"]) for r in edges],
                         [(blocked.id, blocker.id)])

        # The point of rebuilding the edge table: a blocked task must not be
        # handed out as ready work.
        ready = reopened.list_tasks(ready_only=True).tasks
        self.assertNotIn(blocked.id, [task.id for task in ready])

    def test_unknown_status_degrades_to_pending_without_losing_the_task(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        task = store.add_task({"title": "Survivor", "description": "d"})
        store._conn.close()

        self._write_foreign_status(workspace, task.id, "quantum")

        loaded = TaskStore(str(workspace)).load().tasks
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].title, "Survivor")
        self.assertIs(loaded[0].status, TaskStatus.PENDING)


class ColumnBackfillGateTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="backfill-gate-")

    def test_steady_state_connection_does_not_rewrite_every_row(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        for index in range(5):
            store.add_task({"title": f"T{index}", "description": "d"})
        store._conn.close()

        # Second open is already at the current column schema: the backfill is a
        # full table rewrite and must not run again just because we connected.
        reopened = TaskStore(str(workspace))
        calls = {"count": 0}
        original = reopened._backfill_task_columns_from_payload

        def counting_backfill() -> None:
            calls["count"] += 1
            original()

        reopened._backfill_task_columns_from_payload = counting_backfill  # type: ignore[method-assign]
        reopened._init_db()

        self.assertEqual(calls["count"], 0)
        self.assertEqual(len(reopened.load().tasks), 5)

    def test_backfill_runs_when_the_column_schema_marker_is_missing(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        task = store.add_task({"title": "T", "description": "d"})
        store._conn.execute("DELETE FROM metadata WHERE key = 'column_schema_version'")
        store._conn.execute("UPDATE tasks SET status = 'stale' WHERE id = ?", (task.id,))
        store._conn.commit()
        store._conn.close()

        reopened = TaskStore(str(workspace))
        row = reopened._conn.execute("SELECT status FROM tasks WHERE id = ?", (task.id,)).fetchone()
        self.assertEqual(row["status"], "pending")


if __name__ == "__main__":
    unittest.main()
