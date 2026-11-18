"""
The godot-script runner (TEST-GDSCRIPT-001).

BrickWild's QA is `tests/run_all.gd` (a SceneTree script with named lanes), not
GUT or gdUnit4, so `waterfree testing` could not run it and agents called Godot
by hand. These tests use a fake Godot executable — a Python script behind a
`.cmd` (Windows) or sh (POSIX) shim, so the launch path is the same shape as a
real engine build — that replays a scenario file and records its argv and pid.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from backend.cli._common import EXIT_DEP_MISSING, EXIT_OK, EXIT_USAGE
from backend.cli.dispatcher import dispatch
from backend.test_support import make_temp_dir
from backend.testing.bounded import pid_alive
from backend.testing.godot_script import (
    parse_lane_listing,
    parse_lane_summaries,
    source_fingerprint,
)

SCRIPT = "res://tests/run_all.gd"

# The shape BrickWild's run_all_impl.gd prints (SuiteResult.summary()).
BRICKWILD_STDOUT = """\
Running libraryquick...
--- libraryquick ---
    elapsed 1.50s

Running massing...
--- massing ---
    elapsed 0.25s
  FAIL wall 3 overhangs its footing by 0.4 m
  FAIL roof ridge is not level

============================================================
  libraryquick       PASS  (12 checked, 0 failures, 0 warnings)
  massing            FAIL  (8 checked, 2 failures, 1 warnings)
============================================================
1 SUITE(S) FAILED -- 2 suites, 20 checks, 2 failures, 1 warnings
"""

ALL_PASS_STDOUT = """\
  libraryquick       PASS  (12 checked, 0 failures, 0 warnings)
  poly               PASS  (4 checked, 0 failures, 0 warnings)
ALL PASS -- 2 suites, 16 checks, 0 failures, 0 warnings
"""

PARSE_ERROR = (
    "SCRIPT ERROR: Parse Error: Expected end of statement after expression.\n"
    "          at: GDScript::reload (res://src/house/plan.gd:41)\n"
)

JUNIT = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="massing">
    <testcase classname="massing" name="footings" time="0.1"/>
    <testcase classname="massing" name="ridges" time="0.2"/>
    <testcase classname="massing" name="overhang" time="0.3">
      <failure message="wall 3 overhangs">plan.gd:41</failure>
    </testcase>
  </testsuite>
</testsuites>
"""

FAKE_GODOT = """\
import json, os, pathlib, sys, time

here = pathlib.Path(__file__).resolve().parent
argv = sys.argv[1:]
if argv == ["--version"]:
    print("4.3.stable.fake")
    sys.exit(0)

(here / "argv.json").write_text(json.dumps(argv), encoding="utf-8")
(here / "pid.txt").write_text(str(os.getpid()), encoding="utf-8")
scenario = json.loads((here / "scenario.json").read_text(encoding="utf-8"))
user_args = argv[argv.index("--") + 1:] if "--" in argv else []
if "--list-lanes" in user_args:
    scenario = scenario.get("list", scenario)
if scenario.get("junit"):
    pathlib.Path(os.environ["WATERFREE_JUNIT_XML"]).write_text(scenario["junit"], encoding="utf-8")
sys.stdout.write(scenario.get("stdout", ""))
sys.stdout.flush()
sys.stderr.write(scenario.get("stderr", ""))
sys.stderr.flush()
if scenario.get("hang"):
    time.sleep(60)
sys.exit(scenario.get("exit", 0))
"""


def _make_fake_godot(directory: Path) -> Path:
    (directory / "fake_godot.py").write_text(FAKE_GODOT, encoding="utf-8")
    if os.name == "nt":
        shim = directory / "godot.cmd"
        shim.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "%~dp0fake_godot.py" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n",
            encoding="utf-8",
        )
    else:
        shim = directory / "godot"
        shim.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "$(dirname "$0")/fake_godot.py" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
    return shim


def _make_project(root: Path) -> Path:
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "run_all.gd").write_text(
        "extends SceneTree\n\nfunc _init() -> void:\n\tquit(0)\n", encoding="utf-8",
    )
    return root


def _git(cwd: Path, *args: str) -> None:
    """Hermetic git: none of the developer's global config (signing, hooks) applies."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


class _FakeGodotCase(unittest.TestCase):
    def setUp(self) -> None:
        self.bin_dir = make_temp_dir(self, prefix="fake-godot-")
        self.godot = _make_fake_godot(self.bin_dir)
        self.ws = _make_project(make_temp_dir(self, prefix="gdscript-ws-"))

    def scenario(self, **scenario) -> None:
        (self.bin_dir / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")

    def cli(self, action: str, *extra: str, script: bool = True) -> tuple[int, object, str]:
        argv = ["testing", action, *extra, "--workspace", str(self.ws),
                "--godot-path", str(self.godot)]
        if script:
            argv += ["--script", SCRIPT]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch(argv)
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None), err.getvalue()

    def recorded_user_args(self) -> list[str]:
        argv = json.loads((self.bin_dir / "argv.json").read_text(encoding="utf-8"))
        return argv[argv.index("--") + 1:] if "--" in argv else []


class LaneParsingTests(_FakeGodotCase):
    def test_lane_lines_become_results_with_failure_detail(self) -> None:
        self.scenario(stdout=BRICKWILD_STDOUT, exit=1)
        code, payload, _ = self.cli("run", "--lane", "lane:geom")

        self.assertEqual(code, 1)
        self.assertEqual(payload["runner"], "godot-script")
        self.assertEqual((payload["passed"], payload["failed"]), (1, 1))
        by_name = {r["name"]: r for r in payload["results"]}
        self.assertAlmostEqual(by_name["libraryquick"]["duration_ms"], 1500.0)
        self.assertIn("overhangs its footing", by_name["massing"]["error"])
        self.assertIn("ridge is not level", by_name["massing"]["error"])

        receipt = payload["receipt"]
        self.assertEqual(receipt["exitCode"], 1)
        self.assertEqual(receipt["script"], SCRIPT)
        self.assertIn("--headless", receipt["command"])
        self.assertEqual(receipt["lanes"][1],
                         {"name": "massing", "status": "FAIL", "checked": 8, "failures": 2, "warnings": 1})
        self.assertTrue(receipt["verdict"].startswith("1 SUITE(S) FAILED"))
        self.assertIn("fingerprint", receipt)
        self.assertEqual(self.recorded_user_args(), ["lane:geom"])

        # Full stdout/stderr are kept in the receipt file, not in the JSON.
        stored = json.loads(Path(receipt["receiptFile"]).read_text(encoding="utf-8"))
        self.assertIn("overhangs its footing", stored["stdout"])

    def test_all_lanes_passing_exits_0(self) -> None:
        self.scenario(stdout=ALL_PASS_STDOUT, exit=0)
        code, payload, _ = self.cli("run")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((payload["passed"], payload["failed"]), (2, 0))

    def test_missing_summary_is_incomplete_never_a_pass(self) -> None:
        self.scenario(
            stdout="Running zzz...\n",
            stderr="unknown suite 'zzz'; known suites: poly, massing; known lanes: lane:geom\n",
            exit=0,
        )
        code, payload, err = self.cli("run", "--lane", "zzz")
        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["reason"], "no_summary")
        self.assertEqual(payload["passed"], 0)
        self.assertTrue(any("unknown suite" in line for line in payload["receipt"]["outputTail"]))
        self.assertIn("incomplete", err)

    def test_nonzero_exit_without_fail_lines_is_a_failure(self) -> None:
        self.scenario(stdout=ALL_PASS_STDOUT, exit=3)
        code, payload, _ = self.cli("run")
        self.assertEqual(code, 1)
        self.assertEqual((payload["passed"], payload["failed"]), (2, 1))
        exit_result = next(r for r in payload["results"] if r["name"] == "godot-script exit code")
        self.assertIn("exited 3", exit_result["error"])

    def test_junit_report_is_preferred_over_console_lines(self) -> None:
        self.scenario(stdout=ALL_PASS_STDOUT, junit=JUNIT, exit=1)
        code, payload, _ = self.cli("run")
        self.assertEqual(code, 1)
        self.assertEqual((payload["passed"], payload["failed"]), (2, 1))
        self.assertTrue(any(r["name"] == "massing::overhang" for r in payload["results"]))
        self.assertIsNotNone(payload["receipt"]["junit"])
        # The console lanes are still on the receipt.
        self.assertEqual(len(payload["receipt"]["lanes"]), 2)

    def test_run_one_passes_the_name_as_a_lane_argument(self) -> None:
        self.scenario(stdout=ALL_PASS_STDOUT, exit=0)
        code, _payload, _ = self.cli("run-one", "massing", "--arg=--seed=3")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.recorded_user_args(), ["massing", "--seed=3"])

    def test_script_from_config_when_no_flag(self) -> None:
        (self.ws / ".waterfree").mkdir()
        (self.ws / ".waterfree" / "config.json").write_text(
            json.dumps({"waterfree": {"godotTestScript": SCRIPT}}), encoding="utf-8",
        )
        self.scenario(stdout=ALL_PASS_STDOUT, exit=0)
        code, payload, _ = self.cli("run", "--runner", "godot-script", script=False)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["receipt"]["script"], SCRIPT)


class ListLanesTests(_FakeGodotCase):
    def test_list_lanes_convention(self) -> None:
        self.scenario(list={"stdout": "LANE lane:geom\nLANE lane:plan\n", "exit": 0})
        code, names, _ = self.cli("list")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(names, ["lane:geom", "lane:plan"])
        self.assertEqual(self.recorded_user_args(), ["--list-lanes"])

    def test_brickwild_unknown_argument_message_still_lists_lanes(self) -> None:
        self.scenario(list={
            "stderr": "unknown suite '--list-lanes'; known suites: poly, massing; "
                      "known lanes: lane:geom, lane:plan\n",
            "exit": 2,
        })
        code, names, _ = self.cli("list")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(names, ["lane:geom", "lane:plan", "poly", "massing"])

    def test_no_lanes_printed_is_exit_4(self) -> None:
        self.scenario(list={"stdout": "hello\n", "exit": 0})
        code, _names, err = self.cli("list")
        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertIn("LANE <name>", err)


class StallTests(_FakeGodotCase):
    def test_parse_error_hang_is_killed_through_the_shim(self) -> None:
        self.scenario(stderr=PARSE_ERROR, hang=True)
        started = time.monotonic()
        code, payload, _ = self.cli("run", "--stall", "2s")
        self.assertLess(time.monotonic() - started, 20)

        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["reason"], "stall")
        self.assertIn("Parse Error", payload["firstError"])
        self.assertEqual(payload["firstErrorLocation"], "res://src/house/plan.gd:41")
        self.assertIn("fingerprint", payload["receipt"])

        pid = int((self.bin_dir / "pid.txt").read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(pid_alive(pid), "the fake engine behind the shim must be killed")


class UsageTests(_FakeGodotCase):
    def test_script_flags_are_refused_for_other_runners(self) -> None:
        code, _payload, err = self.cli("run", "--runner", "unittest", "--lane", "x", script=False)
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--lane", err)

    def test_missing_script_names_the_candidates(self) -> None:
        code, _payload, err = self.cli("run", "--runner", "godot-script", script=False)
        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertIn("--script", err)
        self.assertIn(SCRIPT, err)

    def test_nonexistent_script_is_a_setup_error(self) -> None:
        argv = ["testing", "run", "--workspace", str(self.ws), "--godot-path", str(self.godot),
                "--script", "res://tests/nope.gd"]
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = dispatch(argv)
        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertIn("not found", err.getvalue())


class FingerprintTests(unittest.TestCase):
    def test_head_and_dirty_flag(self) -> None:
        repo = make_temp_dir(self, prefix="fingerprint-")
        (repo / "a.gd").write_text("extends Node\n", encoding="utf-8")
        _git(repo, "init", "-q")
        _git(repo, "add", "a.gd")
        _git(repo, "commit", "-q", "-m", "fixture")

        clean = source_fingerprint(repo)
        self.assertRegex(clean["head"], r"^[0-9a-f]{40}$")
        self.assertFalse(clean["dirty"])

        (repo / "a.gd").write_text("extends Node2D\n", encoding="utf-8")
        (repo / "untracked_report.xml").write_text("<x/>", encoding="utf-8")
        dirty = source_fingerprint(repo)
        self.assertEqual(dirty["head"], clean["head"])
        self.assertTrue(dirty["dirty"])
        self.assertEqual(dirty["dirtyFiles"], 1, "untracked files are not counted")


class ParserUnitTests(unittest.TestCase):
    def test_status_first_lane_lines(self) -> None:
        lanes = parse_lane_summaries("PASS economy (3 checked, 0 failures)\nFAIL: combat (2 checked, 1 failure)\n")
        self.assertEqual([(lane.name, lane.passed) for lane in lanes],
                         [("economy", True), ("combat", False)])

    def test_pass_with_failures_counts_as_failed(self) -> None:
        lanes = parse_lane_summaries("  odd   PASS  (3 checked, 1 failures)\n")
        self.assertFalse(lanes[0].passed)

    def test_listing_prefers_explicit_lane_lines(self) -> None:
        text = textwrap.dedent("""\
            LANE lane:a
            known lanes: lane:z
        """)
        self.assertEqual(parse_lane_listing(text), ["lane:a"])


if __name__ == "__main__":
    unittest.main()
