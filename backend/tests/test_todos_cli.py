import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.cli.dispatcher import dispatch
from backend.cli._common import EXIT_OK, EXIT_USAGE
from backend.test_support import make_temp_dir as make_test_dir
from backend.todo.store import TaskStore


def _run(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = dispatch(argv)
    return exit_code, json.loads(buf.getvalue())


class TodosImportCliTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="todos-cli-")

    def test_import_from_file_creates_tasks(self) -> None:
        workspace = self.make_workspace()
        backlog = workspace / "backlog.json"
        backlog.write_text(json.dumps([
            {"key": "GOV-001", "title": "First", "description": "d1"},
            {"key": "GOV-002", "title": "Second", "description": "d2",
             "dependsOn": [{"key": "GOV-001", "type": "blocks"}]},
        ]), encoding="utf-8")

        exit_code, result = _run([
            "todos", "import", "--workspace", str(workspace), "--file", str(backlog),
        ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(result["errors"], [])

        exit_code, listing = _run(["todos", "list", "--workspace", str(workspace)])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(listing["total"], 2)

    def test_import_from_stdin(self) -> None:
        workspace = self.make_workspace()
        payload = json.dumps({"tasks": [{"key": "GOV-001", "title": "From stdin"}]})

        with patch("sys.stdin", io.StringIO(payload)):
            exit_code, result = _run([
                "todos", "import", "--workspace", str(workspace), "--file", "-",
            ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0]["title"], "From stdin")

    def test_import_reports_errors_and_writes_nothing_on_duplicate_key(self) -> None:
        workspace = self.make_workspace()
        backlog = workspace / "backlog.json"
        backlog.write_text(json.dumps([
            {"key": "GOV-001", "title": "First"},
            {"key": "GOV-001", "title": "Also first"},
        ]), encoding="utf-8")

        exit_code, result = _run([
            "todos", "import", "--workspace", str(workspace), "--file", str(backlog),
        ])

        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertEqual(len(result["errors"]), 1)

        exit_code, listing = _run(["todos", "list", "--workspace", str(workspace)])
        self.assertEqual(listing["total"], 0)

    def test_add_with_duplicate_key_is_a_usage_error(self) -> None:
        workspace = self.make_workspace()
        exit_code, _ = _run([
            "todos", "add", "--workspace", str(workspace),
            "--title", "First", "--description", "d", "--key", "GOV-001",
        ])
        self.assertEqual(exit_code, EXIT_OK)

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = dispatch([
                "todos", "add", "--workspace", str(workspace),
                "--title", "Second", "--description", "d", "--key", "GOV-001",
            ])
        self.assertEqual(exit_code, EXIT_USAGE)

    def test_utf8_stdin_round_trips_for_dry_run_and_import(self) -> None:
        workspace = self.make_workspace()
        payload = (
            b'[{"key":"UTF8-001","title":"Maya\xe2\x80\x99s \xe2\x80\x9cCafe\xcc\x81\xe2\x80\x9d \xe2\x80\x94 plan",'
            b'"description":"Unicode stdin"}]'
        )

        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252")):
            exit_code, dry_run = _run([
                "todos", "import", "--workspace", str(workspace), "--file", "-", "--dry-run",
            ])
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252")):
            exit_code_actual, actual = _run([
                "todos", "import", "--workspace", str(workspace), "--file", "-",
            ])

        expected_title = "Maya’s “Cafe\u0301” — plan"
        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(exit_code_actual, EXIT_OK)
        self.assertEqual(dry_run["created"][0]["title"], expected_title)
        self.assertEqual(actual["created"][0]["title"], expected_title)
        self.assertEqual(TaskStore(str(workspace)).list_tasks().tasks[0].title, expected_title)

    def test_malformed_utf8_and_escaped_surrogates_are_usage_errors(self) -> None:
        workspace = self.make_workspace()
        malformed = b'[{"title":"bad \xff","description":"d"}]'
        escaped_surrogate = b'[{"title":"\\ud800","description":"d"}]'

        for payload in (malformed, escaped_surrogate):
            stderr = io.StringIO()
            with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252")), patch("sys.stderr", stderr):
                exit_code = dispatch([
                    "todos", "import", "--workspace", str(workspace), "--file", "-", "--dry-run",
                ])
            self.assertEqual(exit_code, EXIT_USAGE)
            self.assertIn("error: --file", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(TaskStore(str(workspace)).list_tasks().tasks, [])


class TodosJsonFileCliTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="todos-json-cli-")

    def test_add_from_json_file_accepts_full_task_fields_and_flag_override(self) -> None:
        workspace = self.make_workspace()
        task_file = workspace / "task.json"
        task_file.write_text(json.dumps({
            "key": "JSON-001",
            "title": "Title from file",
            "description": "Description from file",
            "rationale": "Why this exists",
            "priority": "P1",
            "phase": "JSON",
            "owner": {"type": "agent", "name": "file-agent"},
            "taskType": "feature",
            "estimatedMinutes": 25,
            "aiNotes": "Keep this context",
            "acceptanceCriteria": "The complete object is persisted",
            "targetCoord": {"file": "src/old.py", "line": 4, "anchorType": "modify"},
            "contextCoords": [{"file": "docs/context.md", "line": 8, "anchorType": "inspect"}],
        }), encoding="utf-8")

        exit_code, result = _run([
            "todos", "add", "--workspace", str(workspace), "--json-file", str(task_file),
            "--title", "Unicode — title override", "--priority", "P2",
        ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(result["title"], "Unicode — title override")
        self.assertEqual(result["priority"], "P2")
        self.assertEqual(result["rationale"], "Why this exists")
        self.assertEqual(result["taskType"], "feature")
        self.assertEqual(result["targetCoord"]["file"], "src/old.py")
        self.assertEqual(result["contextCoords"][0]["file"], "docs/context.md")

    def test_add_from_json_stdin(self) -> None:
        workspace = self.make_workspace()
        payload = json.dumps({
            "key": "JSON-STDIN-001",
            "title": "Smart ‘quotes’ survive",
            "description": "Multiline\nUnicode • payload",
            "taskType": "spike",
        })

        with patch("sys.stdin", io.StringIO(payload)):
            exit_code, result = _run([
                "todos", "add", "--workspace", str(workspace), "--json-file", "-",
            ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(result["title"], "Smart ‘quotes’ survive")
        self.assertEqual(result["description"], "Multiline\nUnicode • payload")
        self.assertEqual(result["taskType"], "spike")

    def test_update_from_patch_file_merges_with_inline_patch_and_flags(self) -> None:
        workspace = self.make_workspace()
        exit_code, created = _run([
            "todos", "add", "--workspace", str(workspace),
            "--title", "Original", "--description", "Original description",
        ])
        self.assertEqual(exit_code, EXIT_OK)

        patch_file = workspace / "patch.json"
        patch_file.write_text(json.dumps({
            "title": "Updated — full patch",
            "rationale": "Updated rationale",
            "taskType": "refactor",
            "estimatedMinutes": 40,
            "acceptanceCriteria": "The patch file is read exactly",
        }), encoding="utf-8")

        exit_code, result = _run([
            "todos", "update", created["id"], "--workspace", str(workspace),
            "--patch-file", str(patch_file),
            "--patch", json.dumps({"aiNotes": "Inline patch wins too"}),
            "--status", "executing",
        ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(result["title"], "Updated — full patch")
        self.assertEqual(result["rationale"], "Updated rationale")
        self.assertEqual(result["taskType"], "refactor")
        self.assertEqual(result["estimatedMinutes"], 40)
        self.assertEqual(result["aiNotes"], "Inline patch wins too")
        self.assertEqual(result["status"], "executing")

    def test_json_file_inputs_require_objects(self) -> None:
        workspace = self.make_workspace()
        task_file = workspace / "array.json"
        task_file.write_text(json.dumps([]), encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = dispatch([
                "todos", "add", "--workspace", str(workspace), "--json-file", str(task_file),
            ])

        self.assertEqual(exit_code, EXIT_USAGE)

    def test_bom_prefixed_stdin_is_accepted_for_add_and_patch(self) -> None:
        workspace = self.make_workspace()
        add_payload = b'\xef\xbb\xbf{"title":"Cafe\xcc\x81 \xe2\x80\x94 add","description":"d"}'

        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(add_payload), encoding="cp1252")):
            exit_code, created = _run([
                "todos", "add", "--workspace", str(workspace), "--json-file", "-",
            ])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(created["title"], "Cafe\u0301 — add")

        patch_payload = b'\xef\xbb\xbf{"title":"Maya\xe2\x80\x99s \xe2\x80\x9cCafe\xcc\x81\xe2\x80\x9d \xe2\x80\x94 patch"}'
        with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(patch_payload), encoding="cp1252")):
            exit_code, updated = _run([
                "todos", "update", created["id"], "--workspace", str(workspace), "--patch-file", "-",
            ])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(updated["title"], "Maya’s “Cafe\u0301” — patch")


class TodosSchemaCliTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="todos-schema-cli-")

    def test_schema_exposes_task_fields_and_enum_values(self) -> None:
        exit_code, schema = _run(["todos", "schema"])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(schema["required"], ["title", "description"])
        self.assertIn("taskType", schema["properties"])
        self.assertEqual(
            schema["properties"]["taskType"]["enum"],
            ["impl", "test", "spike", "review", "refactor", "protocol", "bug_fix", "feature", "task"],
        )
        self.assertIn("status", schema["properties"])
        self.assertEqual(schema["properties"]["priority"]["enum"], ["P0", "P1", "P2", "P3", "spike"])
        self.assertIn("read-only-context", schema["$defs"]["codeCoord"]["properties"]["anchorType"]["enum"])

    def test_task_types_lists_model_values(self) -> None:
        exit_code, result = _run(["todos", "task-types"])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(
            result["taskTypes"],
            ["impl", "test", "spike", "review", "refactor", "protocol", "bug_fix", "feature", "task"],
        )

    def test_invalid_task_type_error_lists_valid_values(self) -> None:
        workspace = self.make_workspace()
        task_file = workspace / "task.json"
        task_file.write_text(json.dumps({
            "title": "Bad type",
            "description": "d",
            "taskType": "documentation",
        }), encoding="utf-8")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), patch("sys.stderr", stderr):
            exit_code = dispatch([
                "todos", "add", "--workspace", str(workspace),
                "--json-file", str(task_file),
            ])

        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertIn("valid taskType values", stderr.getvalue())
        self.assertIn("impl, test, spike, review, refactor, protocol, bug_fix, feature, task", stderr.getvalue())


class TodosEnvelopeCliTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="todos-envelope-cli-")

    def test_search_returns_tasks_envelope(self) -> None:
        workspace = self.make_workspace()
        exit_code, _ = _run([
            "todos", "add", "--workspace", str(workspace),
            "--title", "Improve login", "--description", "Make auth clearer",
        ])
        self.assertEqual(exit_code, EXIT_OK)

        exit_code, result = _run([
            "todos", "search", "--workspace", str(workspace), "login",
        ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["tasks"][0]["title"], "Improve login")

    def test_get_ready_returns_tasks_envelope(self) -> None:
        workspace = self.make_workspace()
        for title, priority in (("High", "P1"), ("Low", "P3")):
            exit_code, _ = _run([
                "todos", "add", "--workspace", str(workspace),
                "--title", title, "--description", "d", "--priority", priority,
            ])
            self.assertEqual(exit_code, EXIT_OK)

        exit_code, result = _run([
            "todos", "get-ready", "--workspace", str(workspace), "--limit", "1",
        ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["title"], "High")


class TodosValidateCliTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="todos-validate-cli-")

    def test_validate_reports_backlog_errors(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        store.add_task({"title": "Missing description"})
        store.add_task({
            "title": "Missing dependency",
            "description": "d",
            "dependsOn": [{"taskId": "missing-task", "type": "blocks"}],
        })
        cycle_a = store.add_task({"title": "Cycle A", "description": "d"})
        cycle_b = store.add_task({
            "title": "Cycle B",
            "description": "d",
            "dependsOn": [{"taskId": cycle_a.id, "type": "blocks"}],
        })
        store.update_task(cycle_a.id, {"dependsOn": [{"taskId": cycle_b.id, "type": "blocks"}]})
        store.add_task({
            "title": "Ready but blocked",
            "description": "d",
            "blockedReason": "waiting for a human decision",
        })

        exit_code, result = _run(["todos", "validate", "--workspace", str(workspace)])

        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertFalse(result["ok"])
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("missing_description", codes)
        self.assertIn("unresolved_dependency", codes)
        self.assertIn("dependency_cycle", codes)
        self.assertIn("ready_with_blocked_reason", codes)

    def test_validate_clean_backlog_exits_ok(self) -> None:
        workspace = self.make_workspace()
        store = TaskStore(str(workspace))
        store.add_task({"title": "Clean", "description": "d"})

        exit_code, result = _run(["todos", "validate", "--workspace", str(workspace)])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(result["ok"])
        self.assertEqual(result["issues"], [])


if __name__ == "__main__":
    unittest.main()
