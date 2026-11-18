"""Vitest/Jest JSON parsing: skipped, pending and todo tests are not failures (QA-RUNNER-001)."""

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.testing import runners
from backend.testing.errors import NoTestsMatchedError
from backend.testing.runners import _parse_jest_json, _parse_vitest_json


def _vitest_report(statuses: list[str]) -> str:
    # Shape captured from `vitest run --reporter=json` (jest-compatible).
    tests = [
        {
            "ancestorTitles": ["suite"],
            "fullName": f"suite t{i}",
            "title": f"t{i}",
            "status": status,
            "duration": 1 if status == "passed" else None,
            "failureMessages": ["boom"] if status == "failed" else [],
        }
        for i, status in enumerate(statuses)
    ]
    return json.dumps({"testResults": [{"name": "a.test.ts", "assertionResults": tests, "testResults": tests}]})


class JsStatusMappingTests(unittest.TestCase):
    MIXED = ["passed", "skipped", "pending", "todo", "failed"]

    def _check(self, result):
        self.assertEqual(result.passed, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.skipped, 3)

    def test_vitest_mixed_states(self) -> None:
        self._check(_parse_vitest_json(_vitest_report(self.MIXED)))

    def test_jest_mixed_states(self) -> None:
        self._check(_parse_jest_json(_vitest_report(self.MIXED)))

    def test_all_skipped_reports_zero_executed_not_failures(self) -> None:
        result = _parse_vitest_json(_vitest_report(["skipped"] * 874))
        self.assertEqual((result.passed, result.failed, result.skipped), (0, 0, 874))

    def test_disabled_status_is_skipped(self) -> None:
        result = _parse_jest_json(_vitest_report(["passed", "disabled"]))
        self.assertEqual((result.passed, result.failed, result.skipped), (1, 0, 1))


def _fake_js_run(report: str | None, *, returncode: int = 0, stdout: str = ""):
    """A `_run_command` stand-in: writes `report` where vitest was told to, or prints it (jest)."""
    def fake(cmd, *, workspace_path, timeout, limits=None):
        out = stdout
        target = next((a.split("=", 1)[1] for a in cmd if a.startswith("--outputFile=")), None)
        if report is not None:
            if target:
                Path(target).write_text(report, encoding="utf-8")
            else:
                out = report
        return subprocess.CompletedProcess(cmd, returncode, out, "")
    return fake


class FilteredRunOneTests(unittest.TestCase):
    """TEST-VITEST-001: a filtered run reports the filter-skipped tests as skipped.

    FrejasRest came back as 1 pass / 699 'failures' and goblinchess as 873
    pending-reported-as-failures, because `-t` marks every non-matching test
    skipped/pending and those statuses were counted as failures.
    """

    def _run_one(self, runner_cls, report: str | None, name: str = "t0", **kw):
        with TemporaryDirectory() as tmp, \
             mock.patch.object(runners, "node_tool", return_value=["node-tool"]), \
             mock.patch.object(runners, "_run_command", side_effect=_fake_js_run(report, **kw)):
            return runner_cls().run_one(tmp, name)

    def test_vitest_filtered_run_counts_skips_separately(self) -> None:
        report = _vitest_report(["passed", "passed"] + ["skipped"] * 5 + ["pending"] * 2)
        result = self._run_one(runners.VitestRunner, report)
        self.assertEqual((result.passed, result.failed, result.skipped), (2, 0, 7))

    def test_jest_filtered_run_counts_skips_separately(self) -> None:
        report = _vitest_report(["passed"] + ["pending"] * 4)
        result = self._run_one(runners.JestRunner, report)
        self.assertEqual((result.passed, result.failed, result.skipped), (1, 0, 4))

    def test_vitest_nothing_executed_is_a_no_match_error_naming_the_runner(self) -> None:
        report = _vitest_report(["skipped"] * 873)
        with self.assertRaises(NoTestsMatchedError) as ctx:
            self._run_one(runners.VitestRunner, report, name="no such test")
        exc = ctx.exception
        self.assertEqual((exc.runner, exc.filter, exc.available, exc.skipped),
                         ("vitest", "no such test", 873, 873))

    def test_jest_nothing_executed_is_a_no_match_error(self) -> None:
        with self.assertRaises(NoTestsMatchedError) as ctx:
            self._run_one(runners.JestRunner, _vitest_report(["pending"] * 3), name="nope")
        self.assertEqual(ctx.exception.runner, "jest")

    def test_vitest_without_any_report_says_it_may_be_a_crash(self) -> None:
        with self.assertRaises(NoTestsMatchedError) as ctx:
            self._run_one(runners.VitestRunner, None, returncode=1, stdout="Error: failed to load config")
        self.assertIsNone(ctx.exception.available)
        self.assertIn("no parseable JSON report", str(ctx.exception))

    def test_a_real_failure_is_still_a_failure(self) -> None:
        result = self._run_one(runners.VitestRunner, _vitest_report(["failed", "skipped"]))
        self.assertEqual((result.passed, result.failed, result.skipped), (0, 1, 1))


if __name__ == "__main__":
    unittest.main()
