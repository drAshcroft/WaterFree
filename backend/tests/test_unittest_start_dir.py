"""
Tests for where the unittest runner looks for tests.

The start directory used to be the hard-coded string `backend/tests` —
WaterFree's own layout. Detection could therefore select the unittest runner
for a project keeping its tests in `tests/`, and the runner would then discover
nothing there and report a green `0 passed, 0 failed`.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.testing.errors import NoTestsDiscoveredError
from backend.testing.runners import UnittestRunner, unittest_start_dir


def _make_suite(root: Path, relative: str) -> None:
    directory = root.joinpath(*relative.split("/"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "test_example.py").write_text(
        "import unittest\n"
        "\n"
        "class ExampleTests(unittest.TestCase):\n"
        "    def test_it(self) -> None:\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )


class StartDirTests(unittest.TestCase):
    def test_finds_backend_tests(self) -> None:
        with TemporaryDirectory() as tmp:
            _make_suite(Path(tmp), "backend/tests")
            self.assertEqual(unittest_start_dir(tmp), "backend/tests")

    def test_finds_a_plain_tests_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            _make_suite(Path(tmp), "tests")
            self.assertEqual(unittest_start_dir(tmp), "tests")

    def test_prefers_backend_tests_when_both_exist(self) -> None:
        with TemporaryDirectory() as tmp:
            _make_suite(Path(tmp), "tests")
            _make_suite(Path(tmp), "backend/tests")
            self.assertEqual(unittest_start_dir(tmp), "backend/tests")

    def test_empty_directory_does_not_count(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "tests").mkdir()
            self.assertIsNone(unittest_start_dir(tmp))

    def test_no_directory_at_all(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(unittest_start_dir(tmp))


class RunnerHonoursStartDirTests(unittest.TestCase):
    def test_lists_tests_from_a_plain_tests_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            _make_suite(Path(tmp), "tests")
            names = UnittestRunner().list_tests(tmp)
        self.assertTrue(names, "expected the suite under tests/ to be discovered")
        self.assertTrue(any("test_it" in name for name in names))

    def test_runs_tests_from_a_plain_tests_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            _make_suite(Path(tmp), "tests")
            result = UnittestRunner().run_all(tmp)
        self.assertEqual(result.failed, 0)
        self.assertGreater(result.passed, 0)

    def test_workspace_without_a_suite_raises(self) -> None:
        """Never silently discover nothing."""
        with TemporaryDirectory() as tmp:
            with self.assertRaises(NoTestsDiscoveredError):
                UnittestRunner().list_tests(tmp)


if __name__ == "__main__":
    unittest.main()
