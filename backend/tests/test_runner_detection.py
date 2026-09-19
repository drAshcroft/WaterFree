"""
Tests for framework auto-detection and the Playwright adapter.

The detection tests exist for one defect above all: a workspace with no
recognisable framework used to get the unittest runner, which discovered
nothing and reported `0 passed, 0 failed, exit 0`. Every caller reads that as a
green suite. Detection must now fail loudly instead.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.testing.dotnet import DotnetRunner
from backend.testing.errors import NoFrameworkError
from backend.testing.godot import GodotRunner
from backend.testing.npm_scripts import NpmScriptRunner
from backend.testing.playwright_e2e import (
    PlaywrightRunner,
    is_playwright_project,
    parse_playwright_json,
)
from backend.testing.runners import (
    JestRunner,
    PytestRunner,
    UnittestRunner,
    VitestRunner,
    detect_runner,
)


def _package(root: Path, *, scripts: dict | None = None, deps: dict | None = None) -> None:
    (root / "package.json").write_text(
        json.dumps({"scripts": scripts or {}, "devDependencies": deps or {}}),
        encoding="utf-8",
    )


class DetectionOrderTests(unittest.TestCase):
    def test_godot_wins_over_everything(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
            (root / "addons" / "gut").mkdir(parents=True)
            _package(root, deps={"vitest": "^1.0.0"})
            self.assertIsInstance(detect_runner(tmp), GodotRunner)

    def test_pytest_marker(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            self.assertIsInstance(detect_runner(tmp), PytestRunner)

    def test_jest_dependency(self) -> None:
        with TemporaryDirectory() as tmp:
            _package(Path(tmp), deps={"jest": "^29.0.0"})
            self.assertIsInstance(detect_runner(tmp), JestRunner)

    def test_vitest_dependency(self) -> None:
        with TemporaryDirectory() as tmp:
            _package(Path(tmp), deps={"vitest": "^1.0.0"})
            self.assertIsInstance(detect_runner(tmp), VitestRunner)

    def test_vitest_wins_over_playwright(self) -> None:
        """goblinchess has both; the unit suite is the default gate."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _package(root, deps={"vitest": "^1.0.0", "@playwright/test": "^1.44.0"})
            (root / "playwright.config.ts").write_text("export default {}", encoding="utf-8")
            self.assertIsInstance(detect_runner(tmp), VitestRunner)

    def test_npm_scripts_project(self) -> None:
        """Paradoxia: 48 hand-rolled tsx suites, no framework dependency."""
        with TemporaryDirectory() as tmp:
            _package(Path(tmp), scripts={"test:core": "tsx src/tests/core.test.ts"})
            self.assertIsInstance(detect_runner(tmp), NpmScriptRunner)

    def test_python_suite_outranks_incidental_npm_scripts(self) -> None:
        """WaterFree's own shape: smoke `test:*` scripts beside a Python suite.

        Checking npm scripts first handed WaterFree's 344-test Python suite to
        the npm runner, which reported 84 results scraped from two unrelated
        smoke scripts.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _package(root, scripts={
                "test:acp": "node scripts/acp-smoke/run.js",
                "test:sidebar": "python scripts/sidebar-layout/check.py",
            })
            tests = root / "backend" / "tests"
            tests.mkdir(parents=True)
            (tests / "test_thing.py").write_text("", encoding="utf-8")
            self.assertIsInstance(detect_runner(tmp), UnittestRunner)

    def test_dotnet_solution(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "App.sln").write_text("", encoding="utf-8")
            self.assertIsInstance(detect_runner(tmp), DotnetRunner)

    def test_unittest_requires_an_actual_test_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            tests = Path(tmp) / "backend" / "tests"
            tests.mkdir(parents=True)
            (tests / "test_thing.py").write_text("", encoding="utf-8")
            self.assertIsInstance(detect_runner(tmp), UnittestRunner)


class NoFalseGreenTests(unittest.TestCase):
    def test_empty_workspace_raises_instead_of_reporting_zero_tests(self) -> None:
        """The core defect: no framework must not mean 'everything passes'."""
        with TemporaryDirectory() as tmp:
            with self.assertRaises(NoFrameworkError):
                detect_runner(tmp)

    def test_empty_tests_directory_is_not_a_unittest_project(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "tests").mkdir()
            with self.assertRaises(NoFrameworkError):
                detect_runner(tmp)

    def test_error_names_what_was_looked_for(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(NoFrameworkError) as ctx:
                detect_runner(tmp)
        message = str(ctx.exception)
        for expected in ("gdUnit4", "pytest", "vitest", "--runner"):
            self.assertIn(expected, message)


class PlaywrightParsingTests(unittest.TestCase):
    REPORT = json.dumps({
        "suites": [{
            "title": "tests/board.spec.ts",
            "suites": [{
                "title": "Board",
                "specs": [
                    {
                        "title": "renders the opening position",
                        "ok": True,
                        "tests": [{"projectName": "chromium",
                                   "results": [{"status": "passed", "duration": 120}]}],
                    },
                    {
                        "title": "highlights a legal move",
                        "ok": False,
                        "tests": [{"projectName": "chromium",
                                   "results": [{"status": "failed", "duration": 900,
                                                "errors": [{"message": "locator not found"}]}]}],
                    },
                    {
                        "title": "skipped on mobile",
                        "ok": True,
                        "tests": [{"results": [{"status": "skipped"}]}],
                    },
                ],
            }],
            "specs": [],
        }],
    })

    def test_flattens_nested_suites(self) -> None:
        results = parse_playwright_json(self.REPORT)
        self.assertEqual(len(results), 3)
        self.assertEqual(
            results[0].name,
            "[chromium] tests/board.spec.ts > Board > renders the opening position",
        )

    def test_reports_the_failure_message(self) -> None:
        failed = [r for r in parse_playwright_json(self.REPORT) if not r.passed]
        self.assertEqual(len(failed), 1)
        self.assertIn("locator not found", failed[0].error)

    def test_skipped_is_not_a_failure(self) -> None:
        skipped = next(
            r for r in parse_playwright_json(self.REPORT) if "skipped on mobile" in r.name
        )
        self.assertTrue(skipped.passed)

    def test_malformed_report_yields_nothing(self) -> None:
        self.assertEqual(parse_playwright_json("<html>error</html>"), [])

    def test_config_detection(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertFalse(is_playwright_project(tmp))
            (Path(tmp) / "playwright.config.ts").write_text("", encoding="utf-8")
            self.assertTrue(is_playwright_project(tmp))

    def test_playwright_is_never_auto_detected(self) -> None:
        """It must be opt-in via --runner; it needs a dev server and minutes."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "playwright.config.ts").write_text("", encoding="utf-8")
            _package(root, deps={"@playwright/test": "^1.44.0"})
            with self.assertRaises(NoFrameworkError):
                detect_runner(tmp)
        self.assertIsInstance(PlaywrightRunner(), PlaywrightRunner)


if __name__ == "__main__":
    unittest.main()
