"""Lifecycle fields on tasks: dates (TODO-DATES-001), typed blockers
(TODO-BLOCK-001), overwritable state (TODO-STATE-001), and compatibility with
stores written before those fields existed."""

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.cli._common import EXIT_OK, EXIT_USAGE
from backend.cli.dispatcher import dispatch
from backend.cli.todos import AI_NOTES_WARN_AT, STATE_SOFT_CAP
from backend.session.models import Task, TaskStatus
from backend.session.task_models import TaskBlocker
from backend.test_support import make_temp_dir
from backend.todo.store import TaskStore


def _run(argv: list[str]) -> tuple[int, object, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = dispatch(argv)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = make_temp_dir(self, prefix="todos-life-")
        self.store = TaskStore(str(self.workspace))
        self.addCleanup(self.store.close)
        self.ws = ["--workspace", str(self.workspace)]


class LifecycleDateTests(_StoreCase):
    def test_complete_reopen_and_executing_transitions(self) -> None:
        task = self.store.add_task({"title": "T", "description": "d"})
        self.assertIsNone(task.started_at)

        started = self.store.update_task(task.id, {"status": "executing"})
        self.assertIsNotNone(started.started_at)
        first_start = started.started_at

        done = self.store.update_task(task.id, {"status": "complete"})
        self.assertIsNotNone(done.completed_at)
        self.assertEqual(done.started_at, first_start)

        reopened = self.store.update_task(task.id, {"status": "pending"})
        self.assertIsNone(reopened.completed_at)

        restarted = self.store.update_task(task.id, {"status": "executing"})
        self.assertEqual(restarted.started_at, first_start)  # the first start is kept

    def test_explicit_completed_at_in_the_patch_wins(self) -> None:
        task = self.store.add_task({"title": "T", "description": "d"})
        done = self.store.update_task(task.id, {"status": "complete", "completedAt": "2026-01-02T03:04:05+00:00"})
        self.assertEqual(done.completed_at, "2026-01-02T03:04:05+00:00")

    def test_editor_echo_of_the_old_date_does_not_block_reopen(self) -> None:
        """The VS Code board sends every field back on save, including the old completedAt."""
        task = self.store.add_task({"title": "T", "description": "d"})
        done = self.store.update_task(task.id, {"status": "complete"})
        reopened = self.store.update_task(task.id, {"status": "pending", "completedAt": done.completed_at})
        self.assertIsNone(reopened.completed_at)
        redone = self.store.update_task(task.id, {"status": "complete", "completedAt": None})
        self.assertIsNotNone(redone.completed_at)

    def test_unchanged_status_stamps_nothing(self) -> None:
        task = self.store.add_task({"title": "T", "description": "d", "status": "complete"})
        same = self.store.update_task(task.id, {"status": "complete", "phase": "x"})
        self.assertIsNone(same.completed_at)

    def test_recurring_task_still_resets(self) -> None:
        task = self.store.add_task({"title": "T", "description": "d", "timing": "recurring"})
        result = self.store.update_task(task.id, {"status": "complete"})
        self.assertEqual(result.status, TaskStatus.PENDING)
        self.assertIsNone(result.completed_at)

    def test_import_upsert_transition_stamps_dates(self) -> None:
        self.store.add_task({"key": "K-1", "title": "T", "description": "d"})
        result = self.store.import_tasks([{"key": "K-1", "status": "complete"}], upsert=True, dry_run=False)
        self.assertEqual(result.errors, [])
        self.assertIsNotNone(self.store.get_task("K-1").completed_at)

    def test_cli_update_stamps_dates(self) -> None:
        self.store.add_task({"key": "K-1", "title": "T", "description": "d"})
        _, task, _ = _run(["todos", "update", "K-1", *self.ws, "--status", "executing"])
        self.assertIn("startedAt", task)
        _, task, _ = _run(["todos", "update", "K-1", *self.ws, "--status", "complete"])
        self.assertIn("completedAt", task)


class TypedBlockerTests(_StoreCase):
    def seed(self) -> None:
        for key, priority in (("HUMAN-1", "P0"), ("READY-1", "P1"), ("DEP-1", "P2"), ("DEP-2", "P0")):
            self.store.add_task({"key": key, "title": key, "description": "d", "priority": priority})
        # DEP-2 waits on DEP-1 the classic way.
        self.store.update_task(self.store.get_task("DEP-2").id, {"dependsOn": [{"key": "DEP-1"}]})

    def block_human(self) -> dict:
        code, task, err = _run(["todos", "update", "HUMAN-1", *self.ws, "--blocked-on", "human",
                                "--blocked-reason", "owner must pick the village rules"])
        self.assertEqual(code, EXIT_OK, err)
        return task

    def test_typed_blocked_task_is_skipped_by_get_next_and_get_ready(self) -> None:
        self.seed()
        _, nxt, _ = _run(["todos", "get-next", *self.ws])
        self.assertEqual(nxt["key"], "HUMAN-1")

        task = self.block_human()
        self.assertEqual(task["blockedOn"], "human")
        self.assertEqual(task["status"], "pending")  # a blocker is not a status

        _, nxt, _ = _run(["todos", "get-next", *self.ws])
        self.assertEqual(nxt["key"], "READY-1")
        _, ready, _ = _run(["todos", "get-ready", *self.ws, "--fields", "key"])
        self.assertEqual([t["key"] for t in ready["tasks"]], ["READY-1", "DEP-1"])
        _, listed, _ = _run(["todos", "list", *self.ws, "--ready-only", "--fields", "key"])
        self.assertNotIn("HUMAN-1", [t["key"] for t in listed["tasks"]])

    def test_list_blocked_shows_typed_and_dependency_blocked_tasks(self) -> None:
        self.seed()
        self.block_human()
        _, blocked, _ = _run(["todos", "list", *self.ws, "--blocked", "--fields", "key,blockedOn,blockedReason"])
        self.assertEqual({t["key"] for t in blocked["tasks"]}, {"HUMAN-1", "DEP-2"})
        human = next(t for t in blocked["tasks"] if t["key"] == "HUMAN-1")
        self.assertEqual(human["blockedReason"], "owner must pick the village rules")

        _, waiting, _ = _run(["todos", "list", *self.ws, "--blocked-on", "human", "--fields", "key"])
        self.assertEqual([t["key"] for t in waiting["tasks"]], ["HUMAN-1"])
        self.assertEqual(waiting["total"], 1)
        self.assertEqual({t.key for t in self.store.get_blocked_tasks()}, {"DEP-2", "HUMAN-1"})

    def test_validate_accepts_a_typed_blocker_and_downgrades_a_bare_reason(self) -> None:
        self.seed()
        self.block_human()
        code, result, _ = _run(["todos", "validate", *self.ws])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(result["issues"], [])

        self.store.update_task(self.store.get_task("READY-1").id, {"blockedReason": "someone should look"})
        code, result, _ = _run(["todos", "validate", *self.ws])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(result["ok"])
        issue = result["issues"][0]
        self.assertEqual((issue["code"], issue["severity"]), ("ready_with_blocked_reason", "warning"))

        self.store.update_task(self.store.get_task("READY-1").id, {"blockedReason": None, "blockedOn": "external"})
        _, result, _ = _run(["todos", "validate", *self.ws])
        self.assertEqual([i["code"] for i in result["issues"]], ["blocked_on_without_reason"])

    def test_unblock_returns_the_task_to_the_ready_queue(self) -> None:
        self.seed()
        self.block_human()
        code, task, _ = _run(["todos", "update", "HUMAN-1", *self.ws, "--unblock"])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("blockedOn", task)
        self.assertNotIn("blockedReason", task)
        _, nxt, _ = _run(["todos", "get-next", *self.ws])
        self.assertEqual(nxt["key"], "HUMAN-1")

    def test_blocked_on_needs_a_reason_and_unblock_is_exclusive(self) -> None:
        self.seed()
        code, _, err = _run(["todos", "update", "READY-1", *self.ws, "--blocked-on", "external"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--blocked-reason", err)
        code, _, _ = _run(["todos", "update", "READY-1", *self.ws, "--unblock", "--blocked-on", "human"])
        self.assertEqual(code, EXIT_USAGE)
        # An existing reason is enough.
        self.store.update_task(self.store.get_task("READY-1").id, {"blockedReason": "vendor SDK release"})
        code, task, _ = _run(["todos", "update", "READY-1", *self.ws, "--blocked-on", "external"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["blockedOn"], "external")

    def test_import_round_trips_blocked_on_and_rejects_unknown_values(self) -> None:
        backlog = self.workspace / "backlog.json"
        backlog.write_text(json.dumps([{"key": "IMP-1", "title": "T", "description": "d",
                                        "blockedOn": "human", "blockedReason": "decide"}]), encoding="utf-8")
        code, result, _ = _run(["todos", "import", *self.ws, "--file", str(backlog)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(result["created"][0]["blockedOn"], "human")
        self.assertEqual(self.store.get_task("IMP-1").blocked_on, TaskBlocker.HUMAN)

        backlog.write_text(json.dumps([{"key": "IMP-1", "blockedOn": "maybe"}]), encoding="utf-8")
        code, result, _ = _run(["todos", "import", *self.ws, "--file", str(backlog), "--upsert"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("human, external", result["errors"][0]["error"])

        backlog.write_text(json.dumps([{"key": "IMP-1", "blockedOn": None}]), encoding="utf-8")
        code, _, _ = _run(["todos", "import", *self.ws, "--file", str(backlog), "--upsert"])
        self.assertEqual(code, EXIT_OK)
        self.assertIsNone(self.store.get_task("IMP-1").blocked_on)

    def test_unknown_stored_value_reads_as_external_with_a_warning(self) -> None:
        task = Task.from_dict({"title": "T", "blockedOn": "waiting-on-legal"})
        self.assertEqual(task.blocked_on, TaskBlocker.EXTERNAL)
        self.assertTrue(any("blockedOn" in warning for warning in task.parse_warnings))
        self.assertEqual(Task.from_dict({"title": "T", "blockedOn": "owner"}).blocked_on, TaskBlocker.HUMAN)

    def test_dependency_blocking_is_unchanged(self) -> None:
        self.seed()
        dep1 = self.store.get_task("DEP-1")
        self.assertNotIn("DEP-2", [t.key for t in self.store.get_ready_tasks()])
        self.store.update_task(dep1.id, {"status": "complete"})
        self.assertIn("DEP-2", [t.key for t in self.store.get_ready_tasks()])


class StateFieldTests(_StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.add_task({"key": "S-1", "title": "T", "description": "d", "aiNotes": "2026-11-01 tried A"})

    def test_state_flag_and_file_replace_wholesale(self) -> None:
        code, task, _ = _run(["todos", "update", "S-1", *self.ws, "--state", "## verdict\nA fails"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["state"], "## verdict\nA fails")

        state_file = self.workspace / "state.md"
        state_file.write_text("## verdict\nB works — ship it\n## rejected\n- A\n", encoding="utf-8")
        _, task, _ = _run(["todos", "update", "S-1", *self.ws, "--state-file", str(state_file)])
        self.assertEqual(task["state"], "## verdict\nB works — ship it\n## rejected\n- A")

        payload = "## verdict\nC — via stdin ✓".encode("utf-8")
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252")):
            _, task, _ = _run(["todos", "update", "S-1", *self.ws, "--state-file", "-"])
        self.assertEqual(task["state"], "## verdict\nC — via stdin ✓")
        self.assertEqual(task["aiNotes"], "2026-11-01 tried A")  # history untouched

        _, task, _ = _run(["todos", "update", "S-1", *self.ws, "--state", ""])
        self.assertNotIn("state", task)

    def test_get_shows_state_before_notes(self) -> None:
        _run(["todos", "update", "S-1", *self.ws, "--state", "verdict: B"])
        _, task, _ = _run(["todos", "get", "S-1", *self.ws])
        keys = list(task)
        self.assertLess(keys.index("state"), keys.index("aiNotes"))
        _, notes, _ = _run(["todos", "get", "S-1", *self.ws, "--notes-only"])
        self.assertEqual(list(notes)[:5], ["id", "key", "title", "status", "state"])

    def test_get_warns_once_ai_notes_pass_the_threshold(self) -> None:
        _, task, _ = _run(["todos", "get", "S-1", *self.ws])
        self.assertNotIn("warnings", task)
        long_notes = "x" * (AI_NOTES_WARN_AT + 1)
        self.store.update_task(self.store.get_task("S-1").id, {"aiNotes": long_notes})
        _, task, _ = _run(["todos", "get", "S-1", *self.ws])
        self.assertEqual(len(task["warnings"]), 1)
        self.assertIn("update `state`", task["warnings"][0])

        # Appending to an already long log nudges the writer at the moment it matters.
        _, task, _ = _run(["todos", "update", "S-1", *self.ws, "--append-ai-notes", "more"])
        self.assertIn("state", task["warnings"][0])

    def test_oversized_state_is_kept_but_warned_about(self) -> None:
        big = "y" * (STATE_SOFT_CAP + 10)
        code, task, _ = _run(["todos", "update", "S-1", *self.ws, "--state", big])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["state"], big)
        self.assertIn("soft cap", task["warnings"][0])

    def test_state_is_searchable_projectable_and_imported(self) -> None:
        _run(["todos", "update", "S-1", *self.ws, "--state", "rejected: dried-stock herbs"])
        _, found, _ = _run(["todos", "search", "dried stock", *self.ws])
        self.assertEqual(found["total"], 1)
        self.assertTrue(found["tasks"][0]["match"].startswith("state:"))
        _, listed, _ = _run(["todos", "list", *self.ws, "--fields", "key,state"])
        self.assertEqual(listed["tasks"][0], {"key": "S-1", "state": "rejected: dried-stock herbs"})

        backlog = self.workspace / "backlog.json"
        backlog.write_text(json.dumps([{"key": "S-2", "title": "T", "description": "d", "state": "verdict: new"}]),
                           encoding="utf-8")
        code, result, _ = _run(["todos", "import", *self.ws, "--file", str(backlog)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(result["created"][0]["state"], "verdict: new")
        _, full, _ = _run(["todos", "list", *self.ws, "--full"])
        exported = self.workspace / "export.json"
        exported.write_text(json.dumps(full), encoding="utf-8")
        other = make_temp_dir(self, prefix="todos-life-other-")
        code, result, _ = _run(["todos", "import", "--workspace", str(other), "--file", str(exported)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual({t["key"]: t.get("state") for t in result["created"]},
                         {"S-1": "rejected: dried-stock herbs", "S-2": "verdict: new"})


class LegacyStoreCompatibilityTests(unittest.TestCase):
    """A store written before blockedOn/state/workRef existed must open and round-trip."""

    LEGACY_PAYLOAD = {
        "id": "3f1c5c9e-0000-4000-8000-000000000001",
        "key": "OLD-1",
        "title": "Written by an older WaterFree",
        "description": "no blockedOn/state/workRef keys at all",
        "rationale": "",
        "targetCoord": {"file": "src/app.py", "class": None, "method": None, "line": 3, "anchorType": "modify"},
        "contextCoords": [],
        "priority": "P1",
        "phase": "legacy",
        "dependsOn": [],
        "blockedReason": None,
        "owner": {"type": "agent", "name": "claude", "assignedAt": None},
        "taskType": "feature",
        "estimatedMinutes": 30,
        "actualMinutes": None,
        "status": "executing",
        "humanNotes": None,
        "aiNotes": "2026-10-01 halfway",
        "annotations": [],
        "startedAt": "2026-10-01T00:00:00+00:00",
        "completedAt": None,
        "acceptanceCriteria": "it works",
        "trigger": None,
        "timing": "one_time",
    }

    def make_legacy_store(self) -> Path:
        workspace = make_temp_dir(self, prefix="todos-legacy-")
        (workspace / ".waterfree").mkdir()
        conn = sqlite3.connect(workspace / ".waterfree" / "tasks.db")
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, sort_index INTEGER NOT NULL, payload TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                rationale TEXT NOT NULL DEFAULT '', target_file TEXT NOT NULL DEFAULT '',
                target_class TEXT NOT NULL DEFAULT '', target_method TEXT NOT NULL DEFAULT '',
                target_line INTEGER, priority TEXT NOT NULL DEFAULT 'P2', phase TEXT NOT NULL DEFAULT '',
                owner_name TEXT NOT NULL DEFAULT '', owner_type TEXT NOT NULL DEFAULT 'unassigned',
                status TEXT NOT NULL DEFAULT 'pending', started_at TEXT NOT NULL DEFAULT '', session_id TEXT,
                key TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE task_dependencies (
                task_id TEXT NOT NULL, depends_on_task_id TEXT NOT NULL, dep_type TEXT NOT NULL,
                PRIMARY KEY (task_id, depends_on_task_id, dep_type)
            );
            """
        )
        conn.execute(
            "INSERT INTO tasks(id, sort_index, payload, key, title, status, priority, phase, started_at) "
            "VALUES (?, 0, ?, 'OLD-1', ?, 'executing', 'P1', 'legacy', ?)",
            (self.LEGACY_PAYLOAD["id"], json.dumps(self.LEGACY_PAYLOAD), self.LEGACY_PAYLOAD["title"],
             self.LEGACY_PAYLOAD["startedAt"]),
        )
        for key, value in (("version", "1"), ("phases", '["legacy"]'), ("updated_at", "2026-10-01"),
                           ("velocity_log", "[]"), ("column_schema_version", "2")):
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()
        return workspace

    def test_legacy_store_opens_and_round_trips_without_loss(self) -> None:
        workspace = self.make_legacy_store()
        store = TaskStore(str(workspace))
        self.addCleanup(store.close)

        task = store.get_task("OLD-1")
        self.assertEqual(task.parse_warnings, [])
        self.assertIsNone(task.blocked_on)
        self.assertIsNone(task.state)
        self.assertIsNone(task.work_ref)
        as_dict = task.to_dict()
        for field, value in self.LEGACY_PAYLOAD.items():
            self.assertEqual(as_dict[field], value, field)
        self.assertEqual(set(as_dict) - set(self.LEGACY_PAYLOAD), {"blockedOn", "state", "workRef"})

        # A write through the new code keeps every old field and adds the new ones as null.
        store.update_task(task.id, {"priority": "P2"})
        raw = json.loads(store._conn.execute("SELECT payload FROM tasks").fetchone()[0])
        expected = {**self.LEGACY_PAYLOAD, "priority": "P2", "blockedOn": None, "state": None, "workRef": None}
        self.assertEqual(raw, expected)
        self.assertTrue(store.validate().ok)

    def test_new_fields_survive_a_reopen(self) -> None:
        workspace = self.make_legacy_store()
        store = TaskStore(str(workspace))
        store.update_task(self.LEGACY_PAYLOAD["id"], {
            "blockedOn": "external", "blockedReason": "vendor", "state": "verdict: wait",
            "workRef": {"branch": "feature", "worktreePath": "C:/x", "headCommit": "abc"},
        })
        store.close()
        reopened = TaskStore(str(workspace))
        self.addCleanup(reopened.close)
        task = reopened.get_task("OLD-1")
        self.assertEqual(task.blocked_on, TaskBlocker.EXTERNAL)
        self.assertEqual(task.state, "verdict: wait")
        self.assertEqual(task.work_ref.branch, "feature")
        self.assertIsNotNone(task.work_ref.recorded_at)


if __name__ == "__main__":
    unittest.main()
