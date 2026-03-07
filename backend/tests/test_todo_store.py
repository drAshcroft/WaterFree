import shutil
import json
import unittest
import uuid
from pathlib import Path

from backend.session.models import PlanDocument, Task, TaskDependency, TaskStatus
from backend.todo.store import TaskStore

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_task_store_tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class TaskStoreTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        workspace = _TMP_ROOT / uuid.uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        return workspace

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


if __name__ == "__main__":
    unittest.main()
