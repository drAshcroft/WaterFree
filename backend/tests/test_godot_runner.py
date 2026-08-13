"""Tests for the Godot test-runner adapter.

These cover everything that does not require a real engine: discovery, config /
env / PATH resolution, argument construction, and report parsing. The subprocess
call itself is stubbed — see the module docstring in backend/testing/godot.py.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.testing import godot
from backend.testing.godot import (
    GodotError,
    GodotRunner,
    GdUnit4Strategy,
    GutStrategy,
    detect_framework,
    find_godot_project,
    is_godot_project,
    parse_gut_console,
    parse_junit_xml,
    resolve_godot_binary,
    scan_test_names,
)
from backend.testing.runners import GodotRunner as ReExportedGodotRunner, detect_runner


def _make_project(root: Path, *, framework: str | None = "gut", nested: str | None = None) -> Path:
    project = root / nested if nested else root
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    if framework:
        addon = "gdUnit4" if framework == "gdunit4" else "gut"
        (project / "addons" / addon).mkdir(parents=True, exist_ok=True)
    return project


def _make_suite(project: Path, relpath: str, funcs: list[str]) -> Path:
    path = project / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "extends GutTest\n\n" + "\n".join(
        f"func {name}() -> void:\n\tassert_true(true)\n" for name in funcs
    )
    path.write_text(body, encoding="utf-8")
    return path


class ProjectDiscoveryTests(unittest.TestCase):
    def test_finds_project_at_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_project(root)
            self.assertEqual(find_godot_project(tmp), root)

    def test_finds_project_one_level_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _make_project(root, nested="game")
            self.assertEqual(find_godot_project(tmp), project)

    def test_raises_when_no_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GodotError):
                find_godot_project(tmp)

    def test_detects_gdunit4_over_gut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp), framework="gdunit4")
            (project / "addons" / "gut").mkdir(parents=True)
            self.assertEqual(detect_framework(project), "gdunit4")

    def test_no_framework_is_not_a_godot_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(Path(tmp), framework=None)
            self.assertIsNone(detect_framework(Path(tmp)))
            self.assertFalse(is_godot_project(tmp))

    def test_project_with_framework_is_a_godot_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(Path(tmp))
            self.assertTrue(is_godot_project(tmp))


class BinaryResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in godot.GODOT_ENV_VARS:
            os.environ.pop(var, None)

    def test_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "godot.custom.exe"
            exe.write_text("", encoding="utf-8")
            self.assertEqual(resolve_godot_binary(tmp, str(exe)), str(exe))

    def test_config_file_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "godot.windows.editor.double.x86_64.exe"
            exe.write_text("", encoding="utf-8")
            config = Path(tmp) / ".waterfree"
            config.mkdir()
            (config / "config.json").write_text(
                json.dumps({"godotPath": str(exe)}), encoding="utf-8"
            )
            self.assertEqual(resolve_godot_binary(tmp), str(exe))

    def test_env_var_is_used_when_no_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "godot.exe"
            exe.write_text("", encoding="utf-8")
            os.environ["WATERFREE_GODOT"] = str(exe)
            self.assertEqual(resolve_godot_binary(tmp), str(exe))

    def test_falls_through_broken_env_var_to_working_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "godot.exe"
            exe.write_text("", encoding="utf-8")
            os.environ["WATERFREE_GODOT"] = str(Path(tmp) / "missing.exe")
            os.environ["GODOT_BIN"] = str(exe)
            self.assertEqual(resolve_godot_binary(tmp), str(exe))

    def test_configured_but_broken_path_does_not_silently_use_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["WATERFREE_GODOT"] = str(Path(tmp) / "missing.exe")
            with mock.patch.object(godot.shutil, "which", return_value="/usr/bin/godot"):
                with self.assertRaises(GodotError) as ctx:
                    resolve_godot_binary(tmp)
            self.assertIn("missing.exe", str(ctx.exception))

    def test_path_lookup_when_nothing_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(godot.shutil, "which", side_effect=lambda n: "/usr/bin/godot" if n == "godot" else None):
                self.assertEqual(resolve_godot_binary(tmp), "/usr/bin/godot")

    def test_error_names_every_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(godot.shutil, "which", return_value=None):
                with self.assertRaises(GodotError) as ctx:
                    resolve_godot_binary(tmp)
            message = str(ctx.exception)
            self.assertIn("waterfree.godotPath", message)
            for var in godot.GODOT_ENV_VARS:
                self.assertIn(var, message)


class HeadlessFlagTests(unittest.TestCase):
    def test_godot_4_uses_headless(self) -> None:
        with mock.patch.object(godot, "godot_version", return_value=(4, 3)):
            self.assertEqual(godot.headless_flag("godot"), "--headless")

    def test_godot_3_uses_no_window(self) -> None:
        with mock.patch.object(godot, "godot_version", return_value=(3, 5)):
            self.assertEqual(godot.headless_flag("godot"), "--no-window")

    def test_unknown_version_defaults_to_headless(self) -> None:
        with mock.patch.object(godot, "godot_version", return_value=None):
            self.assertEqual(godot.headless_flag("godot"), "--headless")


class TestNameScanTests(unittest.TestCase):
    def test_scans_res_paths_and_test_functions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            _make_suite(project, "test/test_movement.gd", ["test_walks", "test_jumps"])
            _make_suite(project, "test/nested/world_test.gd", ["test_loads"])
            # Helper functions must not be picked up as tests.
            _make_suite(project, "test/test_helpers.gd", ["test_real"])
            (project / "test" / "test_helpers.gd").write_text(
                "extends GutTest\n\nfunc helper() -> void:\n\tpass\n\nfunc test_real() -> void:\n\tpass\n",
                encoding="utf-8",
            )
            names = scan_test_names(project)
            self.assertIn("res://test/test_movement.gd::test_walks", names)
            self.assertIn("res://test/test_movement.gd::test_jumps", names)
            self.assertIn("res://test/nested/world_test.gd::test_loads", names)
            self.assertIn("res://test/test_helpers.gd::test_real", names)
            self.assertNotIn("res://test/test_helpers.gd::helper", names)

    def test_no_test_dir_yields_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            self.assertEqual(scan_test_names(project), [])


JUNIT_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="MovementTest" tests="3" failures="1" time="0.5">
    <testcase name="test_walks" classname="MovementTest" time="0.010"/>
    <testcase name="test_jumps" classname="MovementTest" time="0.020">
      <failure message="expected true but was false">at test_movement.gd:14</failure>
    </testcase>
    <testcase name="test_skipped" classname="MovementTest" time="0.0">
      <skipped/>
    </testcase>
  </testsuite>
</testsuites>
"""


class JUnitParsingTests(unittest.TestCase):
    def test_parses_pass_fail_and_duration(self) -> None:
        results = parse_junit_xml(JUNIT_SAMPLE)
        self.assertEqual(len(results), 3)
        by_name = {r.name: r for r in results}

        walks = by_name["MovementTest::test_walks"]
        self.assertTrue(walks.passed)
        self.assertIsNone(walks.error)
        self.assertAlmostEqual(walks.duration_ms, 10.0)

        jumps = by_name["MovementTest::test_jumps"]
        self.assertFalse(jumps.passed)
        self.assertIn("expected true but was false", jumps.error)
        self.assertIn("test_movement.gd:14", jumps.error)

        skipped = by_name["MovementTest::test_skipped"]
        self.assertTrue(skipped.passed)
        self.assertEqual(skipped.error, "skipped")

    def test_malformed_xml_yields_nothing(self) -> None:
        self.assertEqual(parse_junit_xml("<not xml"), [])


class GutConsoleParsingTests(unittest.TestCase):
    def test_parses_totals_line(self) -> None:
        raw = (
            "Running tests...\n"
            "Totals\n"
            "  Scripts            3\n"
            "  Passing tests      11\n"
            "  Failing tests      2\n"
        )
        result = parse_gut_console(raw)
        self.assertEqual(result.passed, 11)
        self.assertEqual(result.failed, 2)

    def test_no_totals_yields_zeroes(self) -> None:
        result = parse_gut_console("engine crashed")
        self.assertEqual((result.passed, result.failed), (0, 0))


class CommandConstructionTests(unittest.TestCase):
    def test_gut_targets_test_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            (project / "test").mkdir()
            args = GutStrategy().command(project, Path(tmp) / "reports", [])
            self.assertIn("-gexit", args)
            self.assertIn("-gdir=res://test", args)
            self.assertTrue(any(a.startswith("-gjunit_xml_file=") for a in args))

    def test_gut_targets_specific_suites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            (project / "test").mkdir()
            args = GutStrategy().command(
                project, Path(tmp) / "reports", ["res://test/test_movement.gd"]
            )
            self.assertIn("-gtest=res://test/test_movement.gd", args)
            self.assertFalse(any(a.startswith("-gdir=") for a in args))

    def test_gdunit4_targets_test_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp), framework="gdunit4")
            (project / "tests").mkdir()
            report_dir = Path(tmp) / "reports"
            args = GdUnit4Strategy().command(project, report_dir, [])
            self.assertIn("res://addons/gdUnit4/bin/GdUnitCmdTool.gd", args)
            self.assertIn("-a", args)
            self.assertIn("res://tests", args)
            # gdUnit4 halts on first failure without --continue, and '--rd' is
            # not a valid spelling of the report-directory flag.
            self.assertIn("--continue", args)
            self.assertIn("--report-directory", args)
            self.assertIn(str(report_dir), args)
            self.assertNotIn("--rd", args)

    def test_missing_test_dir_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            with self.assertRaises(GodotError):
                GutStrategy().command(project, Path(tmp) / "reports", [])


class RunnerTests(unittest.TestCase):
    """Drives GodotRunner end to end with the engine subprocess stubbed."""

    def _run_with_stub(self, project: Path, workspace: str, *, xml: str | None, stdout: str = ""):
        recorded: dict = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            recorded["cwd"] = kwargs.get("cwd")
            if xml is not None:
                # Mimic the framework writing its report where we asked.
                report_dir = Path(workspace) / ".waterfree" / "testing" / "godot"
                target = next(report_dir.rglob("*"), None)
                base = target if target and target.is_dir() else next(report_dir.iterdir())
                (base / "results.xml").write_text(xml, encoding="utf-8")
            return mock.Mock(stdout=stdout, stderr="", returncode=0)

        with mock.patch.object(godot, "resolve_godot_binary", return_value="godot"), \
             mock.patch.object(godot, "headless_flag", return_value="--headless"), \
             mock.patch.object(godot.subprocess, "run", side_effect=fake_run):
            result = GodotRunner().run_all(workspace)
        return result, recorded

    def test_run_all_parses_junit_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            (project / "test").mkdir()
            result, recorded = self._run_with_stub(project, tmp, xml=JUNIT_SAMPLE)

            self.assertEqual(result.passed, 2)
            self.assertEqual(result.failed, 1)
            self.assertIn("--headless", recorded["cmd"])
            self.assertIn("--path", recorded["cmd"])
            self.assertIn(str(project), recorded["cmd"])

    def test_run_all_falls_back_to_console_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            (project / "test").mkdir()
            stdout = "Totals\n  Passing tests  4\n  Failing tests  0\n"
            result, _ = self._run_with_stub(project, tmp, xml=None, stdout=stdout)
            self.assertEqual(result.passed, 4)
            self.assertEqual(result.failed, 0)

    def test_unparseable_run_is_reported_as_failure_not_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            (project / "test").mkdir()
            result, _ = self._run_with_stub(project, tmp, xml=None, stdout="segfault")
            self.assertEqual(result.failed, 1)
            self.assertFalse(result.results[0].passed)
            self.assertIn("no parseable test report", result.results[0].error)

    def test_run_one_without_match_does_not_launch_godot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            _make_suite(project, "test/test_movement.gd", ["test_walks"])
            with mock.patch.object(godot.subprocess, "run") as spawn:
                result = GodotRunner().run_one(tmp, "nonexistent")
            spawn.assert_not_called()
            self.assertEqual(result.failed, 1)
            self.assertIn("No tests found matching", result.results[0].error)

    def test_list_tests_does_not_launch_godot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            _make_suite(project, "test/test_movement.gd", ["test_walks", "test_jumps"])
            with mock.patch.object(godot.subprocess, "run") as spawn:
                names = GodotRunner().list_tests(tmp)
            spawn.assert_not_called()
            self.assertEqual(len(names), 2)

    def test_missing_framework_is_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(Path(tmp), framework=None)
            with self.assertRaises(GodotError) as ctx:
                GodotRunner().run_all(tmp)
            self.assertIn("gdUnit4", str(ctx.exception))


class DetectRunnerTests(unittest.TestCase):
    def test_godot_project_selects_godot_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(Path(tmp))
            self.assertIsInstance(detect_runner(tmp), ReExportedGodotRunner)

    def test_godot_project_without_framework_does_not_hijack_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(Path(tmp), framework=None)
            (Path(tmp) / "conftest.py").write_text("", encoding="utf-8")
            self.assertNotIsInstance(detect_runner(tmp), ReExportedGodotRunner)


if __name__ == "__main__":
    unittest.main()
