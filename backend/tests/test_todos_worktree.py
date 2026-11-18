"""Todos from linked git worktrees (TODO-WT-001) and recorded work refs (TODO-BRANCH-001).

Every test builds its own throwaway repository with a linked worktree; nothing
touches the WaterFree checkout's own task store.
"""

import io
import json
import os
import shutil
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from backend.cli._common import EXIT_NOT_FOUND, EXIT_OK
from backend.cli.dispatcher import dispatch
from backend.test_support import make_temp_dir
from backend.todo import git_context
from backend.todo.git_context import resolve_todos_workspace
from backend.todo.store import TaskStore

_GIT_ENV = {key: value for key, value in os.environ.items()
            if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, env=_GIT_ENV, check=True,
    )
    return proc.stdout.strip()


def _run(argv: list[str]) -> tuple[int, dict | None, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = dispatch(argv)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


@unittest.skipUnless(shutil.which("git"), "git is required for worktree tests")
class _RepoTestCase(unittest.TestCase):
    """A main checkout on `main` plus a linked worktree on `feature`."""

    def setUp(self) -> None:
        root = make_temp_dir(self, prefix="todos-wt-")
        self.main = root / "main"
        self.main.mkdir()
        _git(self.main, "init", "-q", "-b", "main")
        (self.main / "README.md").write_text("hello\n", encoding="utf-8")
        _git(self.main, "add", "README.md")
        _git(self.main, "commit", "-q", "-m", "init")
        self.worktree = root / "wt"
        _git(self.main, "worktree", "add", "-q", str(self.worktree), "-b", "feature")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)

    def seed_main(self, key: str = "KEY-1") -> None:
        store = TaskStore(str(self.main))
        store.add_task({"key": key, "title": "Main task", "description": "lives in main"})
        store.close()

    def chdir(self, path: Path) -> None:
        os.chdir(path)


class WorktreeStoreResolutionTests(_RepoTestCase):
    def test_worktree_without_store_reads_and_writes_the_main_checkout_store(self) -> None:
        self.seed_main()
        self.chdir(self.worktree)

        code, task, err = _run(["todos", "get", "KEY-1"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(task["title"], "Main task")
        self.assertEqual(task["resolvedFrom"], "worktree")
        self.assertTrue(git_context.same_path(task["workspace"], self.main))

        code, updated, err = _run(["todos", "update", "KEY-1", "--append-ai-notes", "from the worktree"])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(updated["resolvedFrom"], "worktree")

        code, found, _ = _run(["todos", "search", "worktree"])
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["resolvedFrom"], "worktree")

        store = TaskStore(str(self.main))
        self.addCleanup(store.close)
        self.assertEqual(store.get_task("KEY-1").ai_notes, "from the worktree")
        self.assertFalse((self.worktree / ".waterfree").exists())

    def test_subdirectory_of_a_worktree_is_redirected_too(self) -> None:
        self.seed_main()
        sub = self.worktree / "src" / "deep"
        sub.mkdir(parents=True)
        self.chdir(sub)

        code, task, _ = _run(["todos", "get", "KEY-1"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["resolvedFrom"], "worktree")

    def test_explicit_workspace_still_wins(self) -> None:
        self.seed_main()
        other = make_temp_dir(self, prefix="todos-wt-other-")
        other_store = TaskStore(str(other))
        other_store.add_task({"key": "OTHER-1", "title": "Elsewhere", "description": "d"})
        other_store.close()
        self.chdir(self.worktree)

        code, task, _ = _run(["todos", "get", "OTHER-1", "--workspace", str(other)])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("resolvedFrom", task)

        code, _, err = _run(["todos", "get", "KEY-1", "--workspace", str(other)])
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertIn("Task not found: KEY-1", err)

    def test_explicit_workspace_naming_a_storeless_worktree_is_redirected(self) -> None:
        """Agents often pass their own worktree path; that must not create an empty store there."""
        self.seed_main()
        code, task, _ = _run(["todos", "get", "KEY-1", "--workspace", str(self.worktree)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["resolvedFrom"], "worktree")
        self.assertFalse((self.worktree / ".waterfree").exists())

    def test_worktree_with_its_own_store_keeps_using_it(self) -> None:
        self.seed_main()
        local = TaskStore(str(self.worktree))
        local.add_task({"key": "WT-1", "title": "Local", "description": "d"})
        local.close()
        self.chdir(self.worktree)

        code, task, _ = _run(["todos", "get", "WT-1"])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("resolvedFrom", task)

        code, _, _ = _run(["todos", "get", "KEY-1"])
        self.assertEqual(code, EXIT_NOT_FOUND)

    def test_no_store_anywhere_names_the_main_checkout_and_creates_nothing_on_read(self) -> None:
        self.chdir(self.worktree)

        code, payload, err = _run(["todos", "get", "KEY-1"])
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertIsNone(payload)
        self.assertIn("no task store found", err)
        self.assertIn(self.main.as_posix(), err)
        self.assertNotIn("Task not found", err)
        self.assertFalse((self.main / ".waterfree").exists())
        self.assertFalse((self.worktree / ".waterfree").exists())

        code, created, _ = _run(["todos", "add", "--title", "First", "--description", "d", "--key", "NEW-1"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(created["resolvedFrom"], "worktree")
        self.assertTrue((self.main / ".waterfree" / "tasks.db").is_file())
        self.assertFalse((self.worktree / ".waterfree").exists())

    def test_not_found_in_redirected_store_says_where_it_looked(self) -> None:
        self.seed_main()
        self.chdir(self.worktree)
        code, _, err = _run(["todos", "get", "NOPE-9"])
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertIn("Task not found: NOPE-9", err)
        self.assertIn("main checkout", err)

    def test_resolver_leaves_non_worktree_directories_alone(self) -> None:
        plain = make_temp_dir(self, prefix="todos-wt-plain-")
        self.assertEqual(resolve_todos_workspace(str(plain)).path, os.path.abspath(plain))
        resolved = resolve_todos_workspace(str(self.main))
        self.assertFalse(resolved.redirected)
        self.assertTrue(git_context.same_path(resolved.path, self.main))


class WorkRefTests(_RepoTestCase):
    def start_from_worktree(self) -> dict:
        self.seed_main()
        self.chdir(self.worktree)
        code, task, err = _run(["todos", "update", "KEY-1", "--status", "executing"])
        self.assertEqual(code, EXIT_OK, err)
        return task

    def test_executing_from_worktree_records_branch_path_and_head(self) -> None:
        task = self.start_from_worktree()
        ref = task["workRef"]
        self.assertEqual(ref["branch"], "feature")
        self.assertTrue(git_context.same_path(ref["worktreePath"], self.worktree))
        self.assertEqual(ref["headCommit"], _git(self.worktree, "rev-parse", "HEAD"))
        self.assertIn("recordedAt", ref)

        code, listing, _ = _run(["todos", "list", "--fields", "key,workRef"])
        self.assertEqual(listing["tasks"][0]["workRef"]["branch"], "feature")

    def test_get_reports_ahead_count_dirty_files_and_receipt(self) -> None:
        self.start_from_worktree()
        (self.worktree / "feature.txt").write_text("work\n", encoding="utf-8")
        _git(self.worktree, "add", "feature.txt")
        _git(self.worktree, "commit", "-q", "-m", "feature work")
        (self.worktree / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
        receipts = self.worktree / "artifacts" / "KEY-1"
        receipts.mkdir(parents=True)
        (receipts / "receipt.md").write_text("done\n", encoding="utf-8")

        code, task, _ = _run(["todos", "get", "KEY-1"])
        self.assertEqual(code, EXIT_OK)
        live = task["workStatus"]
        self.assertTrue(live["pathExists"])
        self.assertEqual(live["branch"], "feature")
        self.assertEqual(live["base"], "main")
        self.assertEqual(live["ahead"], 1)
        self.assertEqual(live["behind"], 0)
        self.assertEqual(live["dirtyFiles"], 2)  # scratch.txt and artifacts/
        self.assertEqual(live["receipt"]["path"], "artifacts/KEY-1/receipt.md")
        # The stored reference is the start point; live HEAD has moved on.
        self.assertNotEqual(live["headCommit"], task["workRef"]["headCommit"])

    def test_deleted_worktree_degrades_to_stored_values(self) -> None:
        started = self.start_from_worktree()
        (self.worktree / "feature.txt").write_text("work\n", encoding="utf-8")
        _git(self.worktree, "add", "feature.txt")
        _git(self.worktree, "commit", "-q", "-m", "feature work")
        self.chdir(self.main)
        _git(self.main, "worktree", "remove", "--force", str(self.worktree))
        self.assertFalse(self.worktree.exists())

        code, task, err = _run(["todos", "get", "KEY-1", "--workspace", str(self.main)])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(task["workRef"], started["workRef"])
        live = task["workStatus"]
        self.assertFalse(live["pathExists"])
        self.assertTrue(live["branchExists"])  # the branch outlives its worktree
        self.assertEqual(live["ahead"], 1)

        _git(self.main, "branch", "-D", "feature")
        code, task, err = _run(["todos", "get", "KEY-1", "--workspace", str(self.main)])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(task["workStatus"], {"pathExists": False, "branchExists": False})

    def test_main_checkout_edits_do_not_overwrite_the_worktree_pointer(self) -> None:
        started = self.start_from_worktree()
        self.chdir(self.main)
        code, task, _ = _run(["todos", "update", "KEY-1", "--priority", "P1"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["workRef"], started["workRef"])

    def test_another_worktree_cannot_steal_a_live_pointer(self) -> None:
        started = self.start_from_worktree()
        other = self.worktree.parent / "wt2"
        _git(self.main, "worktree", "add", "-q", str(other), "-b", "other")
        self.chdir(other)
        code, task, _ = _run(["todos", "update", "KEY-1", "--append-ai-notes", "seen from wt2"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["workRef"], started["workRef"])

        # ...but completing it from its own worktree refreshes the head commit.
        self.chdir(self.worktree)
        (self.worktree / "f.txt").write_text("x\n", encoding="utf-8")
        _git(self.worktree, "add", "f.txt")
        _git(self.worktree, "commit", "-q", "-m", "finish")
        code, task, _ = _run(["todos", "update", "KEY-1", "--status", "complete"])
        self.assertEqual(task["workRef"]["headCommit"], _git(self.worktree, "rev-parse", "HEAD"))

    def test_pending_task_notes_from_a_worktree_record_nothing(self) -> None:
        self.seed_main()
        self.chdir(self.worktree)
        code, task, _ = _run(["todos", "update", "KEY-1", "--append-ai-notes", "related finding"])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("workRef", task)

    def test_branch_and_worktree_flags_override(self) -> None:
        self.seed_main()
        self.chdir(self.main)
        code, task, _ = _run(["todos", "update", "KEY-1", "--worktree", str(self.worktree)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["workRef"]["branch"], "feature")
        self.assertTrue(git_context.same_path(task["workRef"]["worktreePath"], self.worktree))

        code, task, _ = _run(["todos", "update", "KEY-1", "--branch", "feature", "--status", "executing"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["workRef"]["branch"], "feature")
        self.assertNotIn("worktreePath", task["workRef"])
        self.assertEqual(task["workRef"]["headCommit"], _git(self.main, "rev-parse", "feature"))

    def test_unrelated_invocation_directory_records_nothing(self) -> None:
        self.seed_main()
        elsewhere = make_temp_dir(self, prefix="todos-wt-elsewhere-")
        _git(elsewhere, "init", "-q", "-b", "main")
        self.chdir(elsewhere)
        code, task, _ = _run(["todos", "update", "KEY-1", "--status", "executing",
                              "--workspace", str(self.main)])
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("workRef", task)


if __name__ == "__main__":
    unittest.main()
