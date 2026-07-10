import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.cli.dispatcher import dispatch
from backend.cli._common import EXIT_OK, EXIT_USAGE
from backend.test_support import make_temp_dir as make_test_dir


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


if __name__ == "__main__":
    unittest.main()
