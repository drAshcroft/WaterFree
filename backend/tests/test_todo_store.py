import json
import unittest
from pathlib import Path

from backend.session.models import PlanDocument, Task, TaskDependency, TaskStatus
from backend.test_support import make_temp_dir as make_test_dir
from backend.todo.store import DuplicateKeyError, TaskStore


class TaskStoreTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="todo-store-")

    def test_queue_todo_persists_and_deduplicates(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        first = store.queue_todo(
            file_path=str(workspace / "src" / "app.py"),
            line=12,
            instruction="Handle Redis outage gracefully",
        )
        second = store.queue_todo(
            file_path=str(workspace / "src" / "app.py"),
            line=12,
            instruction="Handle Redis outage gracefully",
        )

        data = store.load()
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(data.tasks), 1)
        self.assertEqual(data.tasks[0].target_coord.file, "src/app.py")

    def test_get_next_task_prefers_higher_priority_ready_work(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        blocked = store.add_task({
            "title": "Blocked",
            "priority": "P0",
            "owner": {"type": "human", "name": "Olive"},
        })
        store.add_task({
            "title": "Ready P1",
            "priority": "P1",
            "owner": {"type": "human", "name": "Olive"},
        })
        ready = store.add_task({
            "title": "Ready P0",
            "priority": "P0",
            "owner": {"type": "unassigned", "name": ""},
        })

        data = store.load()
        data.tasks[0].depends_on.append(TaskDependency(task_id="missing"))
        store.save(data)

        next_task = store.get_next_task(owner_name="Olive")

        self.assertIsNotNone(next_task)
        self.assertEqual(next_task.id, ready.id)

    def test_search_and_update_task_round_trip(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        created = store.add_task({
            "title": "Write tests",
            "description": "Add regression coverage for login flow",
            "phase": "Phase 2",
            "targetCoord": {"file": "tests/test_login.py", "anchorType": "create-at"},
        })
        updated = store.update_task(created.id, {"status": "executing", "owner": {"type": "agent", "name": "codex"}})
        matches = store.search_tasks("login")

        self.assertEqual(updated.status.value, "executing")
        self.assertEqual(updated.owner.name, "codex")
        self.assertEqual(matches[0].id, created.id)

    def test_save_task_board_reorders_tasks_and_preserves_phase_layout(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        first = store.add_task({
            "title": "First task",
            "phase": "Foundation",
        })
        second = store.add_task({
            "title": "Second task",
            "phase": "Polish",
        })
        third = store.add_task({
            "title": "Third task",
            "phase": None,
        })

        data = store.save_task_board(
            layout=[
                {"id": second.id, "phase": "Execution"},
                {"id": third.id, "phase": "Execution"},
                {"id": first.id, "phase": "Foundation"},
            ],
            phases=["Execution", "Foundation", "Wrap Up"],
        )

        self.assertEqual([task.id for task in data.tasks], [second.id, third.id, first.id])
        self.assertEqual(data.tasks[0].phase, "Execution")
        self.assertEqual(data.tasks[1].phase, "Execution")
        self.assertEqual(data.tasks[2].phase, "Foundation")
        self.assertEqual(data.phases, ["Execution", "Foundation", "Wrap Up"])

    def test_save_task_board_applies_priority_from_layout(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        first = store.add_task({"title": "First task", "priority": "P2"})
        second = store.add_task({"title": "Second task", "priority": "P2"})

        data = store.save_task_board(
            layout=[
                {"id": second.id, "priority": "P0"},
                {"id": first.id},
            ],
            phases=[],
        )

        self.assertEqual([task.id for task in data.tasks], [second.id, first.id])
        self.assertEqual(data.tasks[0].priority.value, "P0")
        # Tasks whose layout item omits priority keep their existing value.
        self.assertEqual(data.tasks[1].priority.value, "P2")

    def test_sql_backed_priority_phase_owner_and_blocked_queries(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        blocker = store.add_task({
            "title": "Blocker",
            "priority": "P0",
            "phase": "Phase 1",
            "owner": {"type": "human", "name": "Olive"},
        })
        blocked = store.add_task({
            "title": "Blocked task",
            "priority": "P2",
            "phase": "Phase 2",
            "owner": {"type": "agent", "name": "codex"},
            "dependsOn": [{"taskId": blocker.id, "type": "blocks"}],
        })
        store.add_task({
            "title": "Same owner task",
            "priority": "P1",
            "phase": "Phase 2",
            "owner": {"type": "agent", "name": "codex"},
        })

        by_priority = store.get_tasks_by_priority("P1")
        by_phase = store.get_tasks_by_phase("Phase 2")
        by_owner = store.get_tasks_by_owner("codex")
        blocked_tasks = store.get_blocked_tasks()

        self.assertEqual(len(by_priority), 1)
        self.assertEqual({task.phase for task in by_phase}, {"Phase 2"})
        self.assertEqual(len(by_owner), 2)
        self.assertEqual(blocked_tasks[0].id, blocked.id)

    def test_promote_demote_and_import_from_session(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        backlog = store.add_task({
            "title": "Backlog task",
            "status": "pending",
        })
        promoted = store.promote_to_session(backlog.id, "session-1")
        demoted = store.demote_to_backlog(backlog.id)

        session = PlanDocument(
            id="session-2",
            goal_statement="Ship feature",
            workspace_path=str(workspace),
            tasks=[
                Task(title="Session pending"),
                Task(title="Session done", status=TaskStatus.COMPLETE),
            ],
        )
        imported = store.import_from_session(session)
        all_titles = {task.title for task in store.load().tasks}

        self.assertEqual(promoted.status.value, "executing")
        self.assertEqual(demoted.status.value, "pending")
        self.assertEqual(len(imported), 1)
        self.assertIn("Session pending", all_titles)
        self.assertNotIn("Session done", all_titles)

    def test_imports_legacy_tasks_json_into_tasks_db(self) -> None:
        workspace = self.make_workspace()
        pairs_dir = workspace / ".waterfree"
        pairs_dir.mkdir(parents=True, exist_ok=True)
        legacy_path = pairs_dir / "tasks.json"
        legacy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "tasks": [
                        {
                            "id": "legacy-task",
                            "title": "Legacy backlog task",
                            "description": "Migrated from json",
                            "targetCoord": {"file": "src/legacy.py", "anchorType": "modify"},
                            "contextCoords": [],
                            "priority": "P2",
                            "phase": None,
                            "dependsOn": [],
                            "blockedReason": None,
                            "owner": {"type": "unassigned", "name": "", "assignedAt": None},
                            "taskType": "impl",
                            "estimatedMinutes": None,
                            "actualMinutes": None,
                            "status": "pending",
                            "humanNotes": None,
                            "aiNotes": None,
                            "annotations": [],
                            "startedAt": None,
                            "completedAt": None,
                        }
                    ],
                    "phases": ["Legacy"],
                    "updatedAt": "2026-03-07T00:00:00+00:00",
                    "velocityLog": [],
                }
            ),
            encoding="utf-8",
        )

        store = TaskStore(str(workspace))
        data = store.load()

        self.assertEqual(store.path, str(workspace / ".waterfree" / "tasks.db"))
        self.assertEqual(len(data.tasks), 1)
        self.assertEqual(data.tasks[0].id, "legacy-task")
        self.assertEqual(data.phases, ["Legacy"])

    def test_load_tolerates_legacy_inspect_anchor_type(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        created = store.add_task(
            {
                "title": "Review stored context",
                "contextCoords": [{"file": "src/app.py", "anchorType": "inspect"}],
            }
        )

        data = store.load()

        self.assertEqual(data.tasks[0].id, created.id)
        self.assertEqual(data.tasks[0].context_coords[0].anchor_type.value, "read-only-context")

    def test_add_task_rejects_duplicate_key(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        store.add_task({"title": "First", "key": "GOV-001"})
        with self.assertRaises(DuplicateKeyError):
            store.add_task({"title": "Second", "key": "GOV-001"})

    def test_update_task_rejects_duplicate_key(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        store.add_task({"title": "First", "key": "GOV-001"})
        second = store.add_task({"title": "Second"})
        with self.assertRaises(DuplicateKeyError):
            store.update_task(second.id, {"key": "GOV-001"})

    def test_add_task_resolves_dependency_by_key(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        store.add_task({"title": "Blocker", "key": "GOV-001"})
        blocked = store.add_task({
            "title": "Blocked",
            "dependsOn": [{"key": "GOV-001", "type": "blocks"}],
        })

        self.assertEqual(blocked.depends_on[0].task_id, store.search_tasks("Blocker")[0].id)
        ready_ids = {task.id for task in store.get_ready_tasks()}
        self.assertNotIn(blocked.id, ready_ids)

    def test_search_finds_task_by_key(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        created = store.add_task({"title": "Something", "key": "GOV-001"})
        matches = store.search_tasks("gov-001")

        self.assertEqual(matches[0].id, created.id)

    def test_import_tasks_creates_batch(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        result = store.import_tasks(
            [
                {"key": "GOV-001", "title": "First", "description": "d1"},
                {"key": "GOV-002", "title": "Second", "description": "d2",
                 "dependsOn": [{"key": "GOV-001", "type": "blocks"}]},
            ],
            upsert=False,
            dry_run=False,
        )

        self.assertEqual(len(result.created), 2)
        self.assertEqual(result.errors, [])
        data = store.load()
        self.assertEqual(len(data.tasks), 2)
        second = next(t for t in data.tasks if t.key == "GOV-002")
        first = next(t for t in data.tasks if t.key == "GOV-001")
        self.assertEqual(second.depends_on[0].task_id, first.id)

    def test_import_tasks_upsert_updates_existing_key(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        original = store.add_task({"key": "GOV-001", "title": "Original"})

        result = store.import_tasks(
            [{"key": "GOV-001", "title": "Updated"}],
            upsert=True,
            dry_run=False,
        )

        self.assertEqual(result.updated[0].id, original.id)
        self.assertEqual(result.updated[0].title, "Updated")
        self.assertEqual(len(store.load().tasks), 1)

    def test_import_tasks_without_upsert_rejects_existing_key(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        store.add_task({"key": "GOV-001", "title": "Original"})

        result = store.import_tasks(
            [{"key": "GOV-001", "title": "Updated"}],
            upsert=False,
            dry_run=False,
        )

        self.assertEqual(result.created, [])
        self.assertEqual(result.updated, [])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(store.load().tasks[0].title, "Original")

    def test_import_tasks_rejects_duplicate_key_within_file_and_writes_nothing(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        result = store.import_tasks(
            [
                {"key": "GOV-001", "title": "First"},
                {"key": "GOV-001", "title": "Also first"},
            ],
            upsert=False,
            dry_run=False,
        )

        self.assertEqual(len(result.errors), 1)
        self.assertEqual(store.load().tasks, [])

    def test_import_tasks_rejects_unresolved_dependency_and_writes_nothing(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        result = store.import_tasks(
            [{"key": "GOV-002", "title": "Blocked",
              "dependsOn": [{"key": "GOV-999", "type": "blocks"}]}],
            upsert=False,
            dry_run=False,
        )

        self.assertEqual(len(result.errors), 1)
        self.assertEqual(store.load().tasks, [])

    def test_import_tasks_dry_run_writes_nothing(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))

        result = store.import_tasks(
            [{"key": "GOV-001", "title": "First"}],
            upsert=False,
            dry_run=True,
        )

        self.assertEqual(len(result.created), 1)
        self.assertEqual(store.load().tasks, [])


if __name__ == "__main__":
    unittest.main()
