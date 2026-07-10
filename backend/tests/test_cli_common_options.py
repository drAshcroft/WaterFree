import io
import json
import unittest
from contextlib import redirect_stdout

from backend.cli._common import EXIT_OK
from backend.cli.dispatcher import dispatch
from backend.test_support import make_temp_dir as make_test_dir


def _run(argv: list[str]) -> tuple[int, object]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = dispatch(argv)
    return exit_code, json.loads(buf.getvalue())


class CliCommonOptionsTests(unittest.TestCase):
    def test_knowledge_search_accepts_workspace_and_full(self) -> None:
        workspace = make_test_dir(self, prefix="cli-common-options-")

        exit_code, result = _run([
            "knowledge", "search", "__waterfree_no_such_entry__",
            "--workspace", str(workspace),
            "--full",
        ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertIsInstance(result, dict)
        self.assertIn("entries", result)
        self.assertIn("total", result)

    def test_testing_list_accepts_full(self) -> None:
        workspace = make_test_dir(self, prefix="cli-common-testing-")

        exit_code, result = _run([
            "testing", "list", "--workspace", str(workspace), "--full",
        ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    unittest.main()
