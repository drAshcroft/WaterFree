"""CLI flag coverage (TODO-CLI-001), stdout-only advisories (TODO-STDERR-001)
and bulk delete / prune (TODO-BULK-001)."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from backend.cli._common import EXIT_INTERNAL, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE
from backend.cli.dispatcher import dispatch
from backend.test_support import make_temp_dir
from backend.todo.store import TaskStore


def _run(argv: list[str]) -> tuple[int, object, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = dispatch(argv)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = make_temp_dir(self, prefix="todos-flags-")
        self.ws = ["--workspace", str(self.workspace)]
        self.store = TaskStore(str(self.workspace))
        self.addCleanup(self.store.close)

    def add(self, key: str, **extra) -> None:
        self.store.add_task({"key": key, "title": f"Task {key}", "description": "d", **extra})


class FieldsProjectionTests(_Case):
    def test_fields_on_search_get_get_next_and_get_ready(self) -> None:
        self.add("F-1", priority="P1", phase="alpha")
        for argv in (
            ["todos", "search", "F-1", "--fields", "key,priority,phase"],
            ["todos", "get-ready", "--fields", "key,priority,phase"],
            ["todos", "list", "--fields", "key,priority,phase"],
        ):
            code, result, _ = _run([*argv, *self.ws])
            self.assertEqual(code, EXIT_OK, argv)
            self.assertEqual(result["tasks"], [{"key": "F-1", "priority": "P1", "phase": "alpha"}], argv)
        for argv in (["todos", "get", "F-1", "--fields", "key,priority,phase"],
                     ["todos", "get-next", "--fields", "key,priority,phase"]):
            code, result, _ = _run([*argv, *self.ws])
            self.assertEqual(result, {"key": "F-1", "priority": "P1", "phase": "alpha"}, argv)

    def test_unknown_field_is_a_usage_error(self) -> None:
        self.add("F-1")
        code, _, err = _run(["todos", "get", "F-1", *self.ws, "--fields", "key,nonsense"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("nonsense", err)


class NotesFlagTests(_Case):
    def test_get_notes_only_and_tail(self) -> None:
        self.add("N-1", aiNotes="p1\n\np2\n\np3", humanNotes="h1", state="verdict: ok")
        code, notes, _ = _run(["todos", "get", "N-1", *self.ws, "--notes-only"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(set(notes), {"id", "key", "title", "status", "state", "aiNotes", "humanNotes"})
        self.assertEqual(notes["aiNotes"], "p1\n\np2\n\np3")

        _, tail, _ = _run(["todos", "get", "N-1", *self.ws, "--tail", "2"])
        self.assertEqual(tail["aiNotes"], "p2\n\np3")
        self.assertEqual(tail["humanNotes"], "h1")
        self.assertEqual(tail["truncated"], {"aiNotes": {"shown": 2, "total": 3}})

        code, _, _ = _run(["todos", "get", "N-1", *self.ws, "--notes-only", "--fields", "key"])
        self.assertEqual(code, EXIT_USAGE)
        code, _, _ = _run(["todos", "get", "N-1", *self.ws, "--tail", "0"])
        self.assertEqual(code, EXIT_USAGE)

    def test_append_notes_from_files_and_stdin(self) -> None:
        self.add("N-1", aiNotes="first")
        note = self.workspace / "note.md"
        note.write_text("2026-11-17: \"quoted\" & $dollar — multi\nline\n", encoding="utf-8")
        code, task, _ = _run(["todos", "update", "N-1", *self.ws, "--append-ai-notes-file", str(note)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["aiNotes"], "first\n\n2026-11-17: \"quoted\" & $dollar — multi\nline")

        payload = "human ✓ via stdin".encode("utf-8")
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252")):
            code, task, _ = _run(["todos", "update", "N-1", *self.ws, "--append-human-notes-file", "-"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["humanNotes"], "human ✓ via stdin")

    def test_two_stdin_inputs_and_exclusive_flags_are_rejected(self) -> None:
        self.add("N-1")
        code, _, err = _run(["todos", "update", "N-1", *self.ws,
                             "--state-file", "-", "--append-ai-notes-file", "-"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("stdin", err)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            dispatch(["todos", "update", "N-1", *self.ws, "--append-ai-notes", "a", "--append-ai-notes-file", "f"])

    def test_empty_notes_file_is_an_error(self) -> None:
        self.add("N-1")
        empty = self.workspace / "empty.md"
        empty.write_text("  \n", encoding="utf-8")
        code, _, _ = _run(["todos", "update", "N-1", *self.ws, "--append-ai-notes-file", str(empty)])
        self.assertEqual(code, EXIT_USAGE)


class UpdateFlagTests(_Case):
    def test_key_acceptance_criteria_title_and_description_flags(self) -> None:
        self.add("U-1")
        code, task, _ = _run(["todos", "update", "U-1", *self.ws, "--key", "U-2",
                              "--acceptance-criteria", "tests pass", "--title", "New title",
                              "--description", "New description"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((task["key"], task["acceptanceCriteria"], task["title"], task["description"]),
                         ("U-2", "tests pass", "New title", "New description"))
        self.add("U-3")
        code, _, err = _run(["todos", "update", "U-2", *self.ws, "--key", "U-3"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("already in use", err)

    def test_bad_flag_prints_the_subcommand_usage(self) -> None:
        self.add("U-1")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            dispatch(["todos", "update", "U-1", *self.ws, "--bogus"])
        self.assertEqual(raised.exception.code, EXIT_USAGE)
        self.assertIn("usage: waterfree todos update", err.getvalue())
        self.assertIn("unrecognized arguments: --bogus", err.getvalue())

    def test_bad_flag_on_other_areas_still_reports_their_usage(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            dispatch(["todos", "list", "--nope"])
        self.assertIn("usage: waterfree todos list", err.getvalue())

    def test_patch_depends_on_resolves_a_key_written_as_task_id(self) -> None:
        self.add("D-1")
        self.add("D-2")
        code, task, _ = _run(["todos", "update", "D-2", *self.ws,
                              "--patch", json.dumps({"dependsOn": [{"taskId": "D-1"}]})])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(task["dependsOn"][0]["taskId"], self.store.get_task("D-1").id)
        code, result, _ = _run(["todos", "validate", *self.ws])
        self.assertEqual(code, EXIT_OK, result)

        code, _, err = _run(["todos", "update", "D-2", *self.ws,
                             "--patch", json.dumps({"dependsOn": [{"taskId": "NOPE-1"}]})])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("unknown task: NOPE-1", err)
        self.assertEqual(self.store.get_task("D-2").depends_on[0].task_id, self.store.get_task("D-1").id)

    def test_import_resolves_dependencies_on_keys_created_in_the_same_file(self) -> None:
        backlog = self.workspace / "backlog.json"
        backlog.write_text(json.dumps([
            {"key": "B-1", "title": "First", "description": "d"},
            {"key": "B-2", "title": "Second", "description": "d", "dependsOn": [{"taskId": "B-1"}]},
            {"key": "B-3", "title": "Third", "description": "d", "dependsOn": ["B-2", {"key": "b-1"}]},
        ]), encoding="utf-8")
        code, result, _ = _run(["todos", "import", *self.ws, "--file", str(backlog)])
        self.assertEqual(code, EXIT_OK, result)
        ids = {t["key"]: t["id"] for t in result["created"]}
        b3 = next(t for t in result["created"] if t["key"] == "B-3")
        self.assertEqual([d["taskId"] for d in b3["dependsOn"]], [ids["B-2"], ids["B-1"]])
        code, _, _ = _run(["todos", "validate", *self.ws])
        self.assertEqual(code, EXIT_OK)


class StdoutOnlyAdvisoryTests(_Case):
    def test_truncated_list_writes_nothing_to_stderr(self) -> None:
        for index in range(3):
            self.add(f"L-{index}")
        code, page, err = _run(["todos", "list", *self.ws, "--limit", "1"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        self.assertTrue(page["truncated"])
        self.assertIn("--offset 1", page["hint"])

    def test_bracketed_key_note_on_add_is_a_json_warning(self) -> None:
        code, task, err = _run(["todos", "add", *self.ws, "--title", "[GOV-001] thing", "--description", "d"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        self.assertIn("not task keys", task["warnings"][0])


class BulkDeleteTests(_Case):
    def seed(self) -> None:
        for key in ("X-1", "X-2", "X-3", "KEEP-1"):
            self.add(key)

    def test_ids_file_deletes_in_one_go_and_accepts_ids_keys_and_comments(self) -> None:
        self.seed()
        ids_file = self.workspace / "ids.txt"
        ids_file.write_text(f"# prune\nX-1\n{self.store.get_task('X-2').id}\n\nx-3\n", encoding="utf-8")
        code, result, _ = _run(["todos", "delete", *self.ws, "--ids-file", str(ids_file)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(result["deletedCount"], 3)
        self.assertEqual(result["tasks"], ["X-1", "X-2", "X-3"])
        self.assertEqual([t.key for t in self.store.load().tasks], ["KEEP-1"])

    def test_unknown_ids_delete_nothing_unless_ignored(self) -> None:
        self.seed()
        ids_file = self.workspace / "ids.txt"
        ids_file.write_text("X-1\nGHOST-1\n", encoding="utf-8")
        code, result, _ = _run(["todos", "delete", *self.ws, "--ids-file", str(ids_file)])
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertEqual(result["notFound"], ["GHOST-1"])
        self.assertEqual(result["deletedCount"], 0)
        self.assertEqual(len(self.store.load().tasks), 4)

        code, result, _ = _run(["todos", "delete", *self.ws, "--ids-file", str(ids_file), "--ignore-missing"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((result["deletedCount"], result["notFound"]), (1, ["GHOST-1"]))
        self.assertIsNone(self.store.get_task("X-1"))

    def test_dry_run_writes_nothing_and_json_list_output_is_accepted(self) -> None:
        self.seed()
        _, listing, _ = _run(["todos", "list", *self.ws, "--fields", "id"])
        with patch("sys.stdin", io.StringIO(json.dumps(listing))):
            code, result, _ = _run(["todos", "delete", *self.ws, "--ids-file", "-", "--dry-run"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((result["matched"], result["deletedCount"], result["dryRun"]), (4, 0, True))
        self.assertEqual(len(self.store.load().tasks), 4)

    def test_dependents_of_deleted_tasks_are_reported(self) -> None:
        self.seed()
        self.store.update_task(self.store.get_task("KEEP-1").id, {"dependsOn": [{"key": "X-1"}]})
        code, result, _ = _run(["todos", "delete", "X-1", *self.ws])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(result["deleted"])
        self.assertEqual(result["orphanedDependents"][0]["key"], "KEEP-1")
        self.assertEqual(result["orphanedDependents"][0]["dependsOn"], ["X-1"])

    def test_argument_errors(self) -> None:
        self.seed()
        self.assertEqual(_run(["todos", "delete", *self.ws])[0], EXIT_USAGE)
        self.assertEqual(_run(["todos", "delete", "X-1", *self.ws, "--ids-file", "f"])[0], EXIT_USAGE)


class PruneTests(_Case):
    def seed(self) -> None:
        self.add("OLD-1", status="complete", completedAt="2026-01-01T00:00:00+00:00")
        self.add("OLD-2", status="complete")  # closed before dates were recorded
        self.add("NEW-1", status="complete", completedAt="2999-01-01T00:00:00+00:00")
        self.add("SKIP-1", status="skipped")
        self.add("OPEN-1")

    def test_prune_exports_then_deletes(self) -> None:
        self.seed()
        export = self.workspace / "history" / "done.json"
        code, result, err = _run(["todos", "prune", *self.ws, "--status", "complete", "--export", str(export)])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(result["prunedCount"], 3)
        self.assertEqual(result["exportedCount"], 3)
        exported = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual({t["key"] for t in exported["tasks"]}, {"OLD-1", "OLD-2", "NEW-1"})
        self.assertEqual({t.key for t in self.store.load().tasks}, {"SKIP-1", "OPEN-1"})

        # Pruning again into the same file extends the history rather than replacing it.
        code, result, _ = _run(["todos", "prune", *self.ws, "--status", "skipped", "--export", str(export)])
        self.assertEqual(code, EXIT_OK)
        exported = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual({t["key"] for t in exported["tasks"]}, {"OLD-1", "OLD-2", "NEW-1", "SKIP-1"})

        # The export is a valid import file.
        other = make_temp_dir(self, prefix="todos-flags-restore-")
        code, restored, _ = _run(["todos", "import", "--workspace", str(other), "--file", str(export)])
        self.assertEqual(code, EXIT_OK, restored)
        self.assertEqual(len(restored["created"]), 4)

    def test_older_than_uses_completed_at_and_reports_undated(self) -> None:
        self.seed()
        code, result, _ = _run(["todos", "prune", *self.ws, "--older-than", "30d", "--dry-run"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(result["tasks"], ["OLD-1"])
        self.assertEqual(result["skippedUndated"], 1)
        self.assertIn("--include-undated", result["hint"])

        _, result, _ = _run(["todos", "prune", *self.ws, "--older-than", "30d", "--include-undated", "--dry-run"])
        self.assertEqual(sorted(result["tasks"]), ["OLD-1", "OLD-2"])

    def test_dry_run_writes_nothing(self) -> None:
        self.seed()
        export = self.workspace / "never.json"
        code, result, _ = _run(["todos", "prune", *self.ws, "--export", str(export), "--dry-run"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((result["matched"], result["prunedCount"]), (3, 0))
        self.assertFalse(export.exists())
        self.assertEqual(len(self.store.load().tasks), 5)

    def test_failed_export_deletes_nothing(self) -> None:
        self.seed()
        not_an_export = self.workspace / "notes.json"
        not_an_export.write_text('{"something": "else"}', encoding="utf-8")
        code, _, err = _run(["todos", "prune", *self.ws, "--export", str(not_an_export)])
        self.assertEqual(code, EXIT_INTERNAL)
        self.assertIn("nothing was deleted", err)
        self.assertEqual(len(self.store.load().tasks), 5)
        self.assertEqual(not_an_export.read_text(encoding="utf-8"), '{"something": "else"}')

        directory = self.workspace / "a-directory.json"
        directory.mkdir()
        code, _, _ = _run(["todos", "prune", *self.ws, "--export", str(directory)])
        self.assertNotEqual(code, EXIT_OK)
        self.assertEqual(len(self.store.load().tasks), 5)

    def test_prune_requires_an_export_and_finished_statuses(self) -> None:
        self.seed()
        code, _, err = _run(["todos", "prune", *self.ws])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--export", err)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            dispatch(["todos", "prune", *self.ws, "--status", "pending", "--dry-run"])
        self.assertEqual(_run(["todos", "prune", *self.ws, "--older-than", "soon", "--dry-run"])[0], EXIT_USAGE)

    def test_pruning_a_satisfied_dependency_keeps_the_dependent_ready(self) -> None:
        self.seed()
        self.store.update_task(self.store.get_task("OPEN-1").id, {"dependsOn": [{"key": "OLD-1"}]})
        export = self.workspace / "done.json"
        code, result, _ = _run(["todos", "prune", *self.ws, "--export", str(export)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(result["detachedDependencies"][0]["key"], "OPEN-1")
        self.assertEqual(self.store.get_task("OPEN-1").depends_on, [])
        self.assertEqual(self.store.get_next_task().key, "OPEN-1")
        self.assertTrue(self.store.validate().ok)


if __name__ == "__main__":
    unittest.main()
