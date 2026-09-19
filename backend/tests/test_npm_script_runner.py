"""
Tests for the npm-scripts runner.

Covers the shape that used to be invisible: a project whose tests are plain
`test:*` scripts with no framework dependency (Paradoxia). Detection used to
fall through such a project to the unittest runner and report a green 0/0.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.testing import npm_scripts
from backend.testing.npm_scripts import (
    NpmScriptRunner,
    is_npm_scripts_project,
    parse_script_output,
    test_scripts,
)


def _write_package(root: Path, scripts: dict, deps: dict | None = None) -> None:
    payload = {"name": "fixture", "scripts": scripts}
    if deps:
        payload["devDependencies"] = deps
    (root / "package.json").write_text(json.dumps(payload), encoding="utf-8")


class ScriptDiscoveryTests(unittest.TestCase):
    def test_prefers_specific_scripts_over_the_aggregate(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_package(Path(tmp), {
                "test": "npm run test:a && npm run test:b",
                "test:a": "tsx src/tests/a.test.ts",
                "test:b": "tsx src/tests/b.test.ts",
            })
            self.assertEqual(test_scripts(tmp), ["test:a", "test:b"])

    def test_falls_back_to_a_bare_test_script(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_package(Path(tmp), {"test": "node run-tests.js"})
            self.assertEqual(test_scripts(tmp), ["test"])

    def test_aggregate_only_project_reports_nothing_to_run(self) -> None:
        """A script that just chains missing siblings is not a suite."""
        with TemporaryDirectory() as tmp:
            _write_package(Path(tmp), {"test": "npm run a && npm run b"})
            self.assertEqual(test_scripts(tmp), [])

    def test_handles_a_bom(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text(
                json.dumps({"scripts": {"test:x": "tsx x.ts"}}), encoding="utf-8-sig",
            )
            self.assertEqual(test_scripts(tmp), ["test:x"])

    def test_missing_package_json_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(test_scripts(tmp), [])


class DetectionTests(unittest.TestCase):
    def test_framework_project_is_not_an_npm_scripts_project(self) -> None:
        """Vitest has its own adapter; it must win."""
        with TemporaryDirectory() as tmp:
            _write_package(Path(tmp), {"test": "vitest run"}, deps={"vitest": "^1.0.0"})
            self.assertFalse(is_npm_scripts_project(tmp))

    def test_frameworkless_script_project_is_detected(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_package(Path(tmp), {"test:core": "tsx src/tests/core.test.ts"})
            self.assertTrue(is_npm_scripts_project(tmp))


class OutputParsingTests(unittest.TestCase):
    """Against the real format Paradoxia's hand-rolled harness emits."""

    PARADOXIA = (
        "── Duel scene model handoff ────────────────────\n"
        "  ✓  a launcher-prepared seeded Duel reaches the scene unchanged\n"
        "  ✓  a stale puzzle registry model is never adopted\n"
        "\n"
        "2/2 tests passed, 0 failed\n"
    )

    def test_parses_tick_lines(self) -> None:
        results = parse_script_output("test:rng", self.PARADOXIA, exit_code=0)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.passed for r in results))
        self.assertEqual(
            results[0].name,
            "test:rng > a launcher-prepared seeded Duel reaches the scene unchanged",
        )

    def test_totals_line_is_not_mistaken_for_a_test(self) -> None:
        results = parse_script_output("test:rng", self.PARADOXIA, exit_code=0)
        self.assertNotIn(
            "tests passed", " ".join(r.name for r in results),
        )

    def test_parses_a_failure_with_its_detail(self) -> None:
        raw = (
            "  ✓  first thing works\n"
            "  ✗  second thing works\n"
            "       expected 3 to equal 4\n"
            "1/2 tests passed, 1 failed\n"
        )
        results = parse_script_output("test:x", raw, exit_code=1)
        failed = [r for r in results if not r.passed]
        self.assertEqual(len(failed), 1)
        self.assertIn("expected 3 to equal 4", failed[0].error)

    def test_nonzero_exit_is_authoritative_over_parsed_passes(self) -> None:
        """A harness that dies after printing ticks must not come back green."""
        raw = "  ✓  a thing\nSegmentation fault\n"
        results = parse_script_output("test:x", raw, exit_code=139)
        self.assertTrue(any(not r.passed for r in results))

    def test_unrecognised_output_yields_no_per_test_results(self) -> None:
        self.assertEqual(parse_script_output("test:x", "built ok\n", exit_code=0), [])


class RunnerTests(unittest.TestCase):
    def _runner_with(self, outputs: dict[str, tuple[int, str]]):
        """An NpmScriptRunner whose subprocess calls are stubbed."""
        def fake_run(cmd, *, workspace_path, timeout, env=None):
            script = cmd[-1]
            code, text = outputs[script]
            return mock.Mock(returncode=code, stdout=text, stderr="")

        return mock.patch.object(npm_scripts, "run_tool", side_effect=fake_run)

    def test_runs_every_script_and_aggregates(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_package(Path(tmp), {
                "test:a": "tsx a.ts",
                "test:b": "tsx b.ts",
            })
            outputs = {
                "test:a": (0, "  ✓  alpha\n"),
                "test:b": (1, "  ✗  beta\n       boom\n"),
            }
            with self._runner_with(outputs), \
                 mock.patch.object(npm_scripts, "npm", return_value=r"C:\node\npm.CMD"):
                result = NpmScriptRunner(workers=1).run_all(tmp)

        self.assertEqual(result.passed, 1)
        self.assertEqual(result.failed, 1)
        self.assertIn("npm run test:a", result.raw_output)

    def test_run_one_filters_by_script_name(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_package(Path(tmp), {"test:rng": "tsx rng.ts", "test:ai": "tsx ai.ts"})
            with self._runner_with({"test:rng": (0, "  ✓  seeded\n")}), \
                 mock.patch.object(npm_scripts, "npm", return_value=r"C:\node\npm.CMD"):
                result = NpmScriptRunner(workers=1).run_one(tmp, "rng")

        self.assertEqual(result.passed, 1)
        self.assertEqual(result.failed, 0)

    def test_run_one_reports_a_miss_as_a_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_package(Path(tmp), {"test:rng": "tsx rng.ts"})
            result = NpmScriptRunner(workers=1).run_one(tmp, "nothing-like-this")
        self.assertEqual(result.failed, 1)

    def test_script_without_recognisable_output_still_reports_one_result(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_package(Path(tmp), {"test": "node run.js"})
            with self._runner_with({"test": (0, "everything fine\n")}), \
                 mock.patch.object(npm_scripts, "npm", return_value=r"C:\node\npm.CMD"):
                result = NpmScriptRunner(workers=1).run_all(tmp)
        self.assertEqual((result.passed, result.failed), (1, 0))


if __name__ == "__main__":
    unittest.main()
