"""
run-one that matches nothing is a selection problem (exit 4), not a red test
(TEST-NOMATCH-001), and in a polyglot repo it names the other runners that
could own the test (TEST-DETECT-001).

Before: dotnet, jest, vitest, unittest, npm-scripts, godot and playwright all
answered a zero-match with `passed=0, failed=1` and a synthetic failing result,
so the CLI exited 1 and agents chased a non-bug. In C:/Projects/combat the
auto-picked unittest runner "failed" a C# test name that dotnet would have run.

unittest and pytest are driven for real (they only need this interpreter);
dotnet and playwright have their tool invocation stubbed. jest, vitest, npm
and godot are covered in their own test modules.
"""

from __future__ import annotations

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from backend.cli._common import EXIT_DEP_MISSING, EXIT_OK
from backend.cli.dispatcher import dispatch
from backend.test_support import make_temp_dir
from backend.testing import dotnet, playwright_e2e
from backend.testing.errors import NoFrameworkError, NoTestsMatchedError
from backend.testing import results as testing_results
from backend.testing.runners import (
    PytestRunner,
    UnittestRunner,
    detect_all_runners,
    detect_runner,
)

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.9.0" />
    <PackageReference Include="xunit" Version="2.7.0" />
  </ItemGroup>
</Project>
"""

CS_TESTS = """namespace Combat.Tests;

public class ParryTests
{
    [Fact]
    public void ParryWindowOpensOnBlock()
    {
    }
}
"""

PY_TESTS = """\
import unittest


class MathTests(unittest.TestCase):
    def test_adds(self):
        self.assertEqual(1 + 1, 2)

    def test_subtracts(self):
        self.assertEqual(2 - 1, 1)

    def test_is_red(self):
        self.assertEqual(1, 2)
"""


def _cli(*argv: str) -> tuple[int, dict | list | None, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = dispatch(list(argv))
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


def _python_suite(root: Path, *, pytest_marker: bool = False) -> None:
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_math.py").write_text(PY_TESTS, encoding="utf-8")
    if pytest_marker:
        (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")


def _dotnet_project(root: Path, *, solution: str | None = None) -> None:
    project = root / "Combat.Tests"
    project.mkdir(parents=True, exist_ok=True)
    (project / "Combat.Tests.csproj").write_text(CSPROJ, encoding="utf-8")
    (project / "ParryTests.cs").write_text(CS_TESTS, encoding="utf-8")
    if solution:
        (root / solution).write_text("", encoding="utf-8")


class UnittestNoMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = make_temp_dir(self, prefix="nomatch-unittest-")
        _python_suite(self.ws)

    def test_runner_raises_with_runner_filter_and_count(self) -> None:
        with self.assertRaises(NoTestsMatchedError) as ctx:
            UnittestRunner().run_one(str(self.ws), "no_such_test")
        exc = ctx.exception
        self.assertEqual((exc.runner, exc.filter, exc.available), ("unittest", "no_such_test", 3))

    def test_cli_exits_4_with_a_json_error(self) -> None:
        code, payload, err = _cli("testing", "run-one", "no_such_test",
                                  "--workspace", str(self.ws), "--runner", "unittest")
        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertEqual(payload["code"], "no_tests_matched")
        self.assertEqual(payload["runner"], "unittest")
        self.assertEqual(payload["filter"], "no_such_test")
        self.assertEqual(payload["available"], 3)
        # Forced runner: no polyglot hint.
        self.assertNotIn("otherRunners", payload)
        self.assertIn("no tests matched", err)

    def test_a_real_failing_test_still_exits_1(self) -> None:
        code, payload, _ = _cli("testing", "run-one", "test_is_red",
                                "--workspace", str(self.ws), "--runner", "unittest")
        self.assertEqual(code, 1)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["runner"], "unittest")

    def test_a_passing_match_exits_0(self) -> None:
        code, payload, _ = _cli("testing", "run-one", "test_adds",
                                "--workspace", str(self.ws), "--runner", "unittest")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((payload["passed"], payload["failed"]), (1, 0))


class PytestNoMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = make_temp_dir(self, prefix="nomatch-pytest-")
        _python_suite(self.ws, pytest_marker=True)

    def test_deselect_all_is_a_no_match_with_the_collected_count(self) -> None:
        """`-k` deselecting everything prints neither 'no tests ran' nor 'collected 0 items'."""
        with self.assertRaises(NoTestsMatchedError) as ctx:
            PytestRunner().run_one(str(self.ws), "no_such_test")
        self.assertEqual((ctx.exception.runner, ctx.exception.available), ("pytest", 3))

    def test_cli_failing_test_exits_1_and_counts_passes_correctly(self) -> None:
        """The verbose lines are parsed, so one failure no longer zeroes `passed`."""
        code, payload, _ = _cli("testing", "run-one", "test_", "--workspace", str(self.ws))
        self.assertEqual(code, 1)
        self.assertEqual((payload["passed"], payload["failed"]), (2, 1))
        red = next(r for r in payload["results"] if not r["passed"])
        self.assertIn("test_is_red", red["name"])


class DotnetNoMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = make_temp_dir(self, prefix="nomatch-dotnet-")
        _dotnet_project(self.ws)

    def _run_one(self, *, stdout: str, returncode: int):
        def fake(cmd, *, workspace_path, timeout, env=None, limits=None):
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")
        with mock.patch.object(dotnet, "dotnet", return_value="dotnet"), \
             mock.patch.object(dotnet, "run_tool", side_effect=fake):
            return dotnet.DotnetRunner().run_one(str(self.ws), "NoSuchTest")

    def test_filter_matching_nothing_is_a_no_match(self) -> None:
        with self.assertRaises(NoTestsMatchedError) as ctx:
            self._run_one(
                stdout="No test matches the given testcase filter `FullyQualifiedName~NoSuchTest` in x.dll",
                returncode=0,
            )
        self.assertEqual((ctx.exception.runner, ctx.exception.available), ("dotnet", 1))

    def test_build_failure_is_a_red_result_not_a_no_match(self) -> None:
        result = self._run_one(
            stdout="ParryTests.cs(7,5): error CS1002: ; expected [Combat.Tests.csproj]\nBuild FAILED.\n",
            returncode=1,
        )
        self.assertEqual(result.failed, 1)
        self.assertIn("build failed", result.results[0].error)
        self.assertIn("CS1002", result.results[0].error)


class PlaywrightNoMatchTests(unittest.TestCase):
    def test_grep_matching_nothing_is_a_no_match(self) -> None:
        ws = make_temp_dir(self, prefix="nomatch-playwright-")

        def fake(cmd, *, workspace_path, timeout, env=None, limits=None):
            return subprocess.CompletedProcess(cmd, 1, "Error: No tests found", "")

        with mock.patch.object(playwright_e2e, "node_tool", return_value=["playwright"]), \
             mock.patch.object(playwright_e2e, "run_tool", side_effect=fake), \
             self.assertRaises(NoTestsMatchedError) as ctx:
            playwright_e2e.PlaywrightRunner().run_one(str(ws), "checkout")
        self.assertEqual(ctx.exception.runner, "playwright")


class CliBackstopTests(unittest.TestCase):
    def test_a_runner_returning_nothing_executed_exits_4(self) -> None:
        """Any runner that comes back 0/0 (e.g. everything matched was skipped)."""
        ws = make_temp_dir(self, prefix="nomatch-backstop-")
        _python_suite(ws, pytest_marker=True)
        empty = testing_results.TestRunResult(passed=0, failed=0, skipped=2, raw_output="2 skipped")
        with mock.patch.object(PytestRunner, "run_one", return_value=empty):
            code, payload, _ = _cli("testing", "run-one", "test_adds",
                                    "--workspace", str(ws), "--runner", "pytest")
        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertEqual((payload["code"], payload["skipped"]), ("no_tests_matched", 2))


class PolyglotDetectionTests(unittest.TestCase):
    """TEST-DETECT-001."""

    def test_detect_all_lists_every_runner_with_its_evidence(self) -> None:
        ws = make_temp_dir(self, prefix="polyglot-")
        _python_suite(ws)
        _dotnet_project(ws)
        found = {d.name: d.evidence for d in detect_all_runners(str(ws))}
        self.assertEqual(found, {
            "unittest": "tests/ with test_*.py",
            "dotnet": "Combat.Tests/Combat.Tests.csproj",
        })
        # detect_runner keeps its old answer: the first in priority order.
        self.assertIsInstance(detect_runner(str(ws)), UnittestRunner)

    def test_solution_file_is_the_dotnet_evidence(self) -> None:
        ws = make_temp_dir(self, prefix="polyglot-sln-")
        _python_suite(ws)
        _dotnet_project(ws, solution="combat.sln")
        found = {d.name: d.evidence for d in detect_all_runners(str(ws))}
        self.assertEqual(found["dotnet"], "combat.sln")

    def test_auto_run_one_of_a_csharp_name_names_dotnet(self) -> None:
        """The acceptance case: unittest + .csproj, auto-detected run-one of a C# test."""
        ws = make_temp_dir(self, prefix="polyglot-cli-")
        _python_suite(ws)
        _dotnet_project(ws, solution="combat.sln")
        code, payload, _ = _cli("testing", "run-one", "ParryWindow", "--workspace", str(ws))

        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertEqual(payload["runner"], "unittest")
        self.assertTrue(payload["autoDetected"])
        dotnet_entry = next(o for o in payload["otherRunners"] if o["runner"] == "dotnet")
        self.assertEqual(dotnet_entry["evidence"], "combat.sln")
        self.assertEqual(dotnet_entry["matches"], 1)
        self.assertIn("--runner dotnet", dotnet_entry["retry"])
        self.assertIn("--runner dotnet", payload["hint"])

    def test_opt_in_runners_are_listed_but_never_auto_selected(self) -> None:
        ws = make_temp_dir(self, prefix="polyglot-optin-")
        (ws / "project.godot").write_text("config_version=5\n", encoding="utf-8")
        (ws / "tests").mkdir()
        (ws / "tests" / "run_all.gd").write_text("extends SceneTree\n", encoding="utf-8")
        (ws / "playwright.config.ts").write_text("export default {}", encoding="utf-8")

        found = {d.name: d for d in detect_all_runners(str(ws))}
        self.assertFalse(found["playwright"].auto)
        self.assertFalse(found["godot-script"].auto)
        self.assertIn("res://tests/run_all.gd", found["godot-script"].evidence)
        with self.assertRaises(NoFrameworkError) as ctx:
            detect_runner(str(ws))
        self.assertIn("--runner godot-script --script res://tests/run_all.gd", str(ctx.exception))
        self.assertIn("--runner playwright", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
