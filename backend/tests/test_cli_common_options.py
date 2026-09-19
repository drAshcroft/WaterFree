import io
import json
import unittest
from contextlib import redirect_stdout

from backend.cli._common import EXIT_DEP_MISSING, EXIT_OK
from backend.cli.dispatcher import dispatch
from backend.test_support import make_temp_dir as make_test_dir


def _run(argv: list[str]) -> tuple[int, object]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = dispatch(argv)
    return exit_code, json.loads(buf.getvalue())


def _run_raw(argv: list[str]) -> tuple[int, str]:
    """Exit code and stdout, for commands that may legitimately emit nothing."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = dispatch(argv)
    return exit_code, buf.getvalue()


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
        # A workspace with real tests in it. An empty one is no longer a valid
        # fixture here: it exits 4 by design (see the test below).
        tests_dir = workspace / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_example.py").write_text(
            "import unittest\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_it(self) -> None:\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )

        exit_code, result = _run([
            "testing", "list", "--workspace", str(workspace), "--full",
        ])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertIsInstance(result, list)

    def test_testing_list_on_a_frameworkless_workspace_is_not_a_success(self) -> None:
        """A workspace with no tests must not answer with an empty green list.

        This used to exit 0 with `[]`, which every caller reads as "the suite is
        fine". It is now a setup failure (exit 4).
        """
        workspace = make_test_dir(self, prefix="cli-common-empty-")

        exit_code, stdout = _run_raw([
            "testing", "list", "--workspace", str(workspace), "--full",
        ])

        self.assertEqual(exit_code, EXIT_DEP_MISSING)
        self.assertEqual(stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
