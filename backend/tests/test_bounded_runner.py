"""
--budget / --stall and the process-tree kill (TEST-BUDGET-001).

The defect: a GDScript parse error leaves a headless Godot run alive and
silent, and agents only noticed when their own tool timeout fired — minutes
later, with no diagnosis. These tests drive real child processes (Python via
sys.executable, so no Godot/node is needed) and check that a hang is killed
fast, reported as incomplete with the first script error, and leaves no
process behind — including a grandchild, which `subprocess.run(timeout=)`
would have orphaned.
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

from backend.cli._common import EXIT_DEP_MISSING
from backend.cli.dispatcher import dispatch
from backend.test_support import make_temp_dir
from backend.testing import bounded
from backend.testing.bounded import (
    RunLimits,
    first_script_error,
    parse_duration,
    pid_alive,
    run_bounded,
)
from backend.testing.errors import RunIncompleteError

PARSE_ERROR = (
    'SCRIPT ERROR: Parse Error: Identifier "Foo" not declared in the current scope.\n'
    "          at: GDScript::reload (res://scripts/player.gd:12)\n"
)


def _script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _spawning_sleeper(directory: Path, *, preamble: str = "") -> tuple[Path, Path]:
    """A script that starts a sleeping grandchild, records both pids, then hangs silently."""
    pids = directory / "pids.json"
    script = _script(directory, "hang.py", f"""
        import json, os, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open({str(pids)!r}, "w") as handle:
            json.dump([os.getpid(), child.pid], handle)
        {preamble}
        time.sleep(60)
    """)
    return script, pids


def _wait_all_dead(pids: list[int], within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not any(pid_alive(pid) for pid in pids):
            return True
        time.sleep(0.1)
    return not any(pid_alive(pid) for pid in pids)


def _wait_for_file(path: Path, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return
        time.sleep(0.05)


class DurationTests(unittest.TestCase):
    def test_parses_units_and_compounds(self) -> None:
        self.assertEqual(parse_duration("90"), 90)
        self.assertEqual(parse_duration("90s"), 90)
        self.assertEqual(parse_duration("5m"), 300)
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("1.5h"), 5400)
        self.assertAlmostEqual(parse_duration("250ms"), 0.25)
        self.assertEqual(parse_duration(" 2M "), 120)

    def test_rejects_ambiguous_or_empty_or_zero(self) -> None:
        for bad in ("", "abc", "1h30", "5 minutes", "0", "0s", "-5s"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_duration(bad)


class FirstScriptErrorTests(unittest.TestCase):
    def test_godot4_parse_error_with_location_on_the_next_line(self) -> None:
        line, location = first_script_error("Godot Engine v4.3\n" + PARSE_ERROR)
        self.assertIn("Parse Error", line)
        self.assertEqual(location, "res://scripts/player.gd:12")

    def test_godot3_parser_error_on_one_line(self) -> None:
        line, location = first_script_error("res://main.gd:4 - Parser Error: Unexpected token\n")
        self.assertIn("Parser Error", line)
        self.assertEqual(location, "res://main.gd:4")

    def test_first_text_wins_and_none_when_absent(self) -> None:
        line, _ = first_script_error("SCRIPT ERROR: first\n", "SCRIPT ERROR: second\n")
        self.assertIn("first", line)
        self.assertEqual(first_script_error("all fine\n"), (None, None))


class RunBoundedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir(self, prefix="bounded-")

    def test_stall_kills_a_silent_process_and_its_grandchild(self) -> None:
        script, pids_file = _spawning_sleeper(self.tmp)
        started = time.monotonic()
        with self.assertRaises(RunIncompleteError) as ctx:
            run_bounded([sys.executable, str(script)], cwd=str(self.tmp),
                        limits=RunLimits(stall=1.0))
        elapsed = time.monotonic() - started

        self.assertEqual(ctx.exception.reason, "stall")
        self.assertLess(elapsed, 10, "the stall kill must not wait for the 60s sleep")
        _wait_for_file(pids_file)
        pids = json.loads(pids_file.read_text(encoding="utf-8"))
        self.assertTrue(_wait_all_dead(pids), f"processes still alive: {pids}")

    def test_parse_error_then_hang_reports_the_first_error_line(self) -> None:
        script, pids_file = _spawning_sleeper(
            self.tmp,
            preamble=f"sys.stderr.write({PARSE_ERROR!r}); sys.stderr.flush()",
        )
        with self.assertRaises(RunIncompleteError) as ctx:
            run_bounded([sys.executable, str(script)], cwd=str(self.tmp),
                        limits=RunLimits(stall=1.0))
        exc = ctx.exception
        self.assertEqual(exc.reason, "stall")
        self.assertIn("Parse Error", exc.first_error)
        self.assertEqual(exc.first_error_location, "res://scripts/player.gd:12")
        self.assertIn("Parse Error", str(exc))
        self.assertIn("Parse Error", exc.raw_output)
        _wait_for_file(pids_file)
        self.assertTrue(_wait_all_dead(json.loads(pids_file.read_text(encoding="utf-8"))))

    def test_budget_kills_a_process_that_keeps_talking(self) -> None:
        """Output resets the stall clock but not the wall-time budget."""
        script = _script(self.tmp, "chatty.py", """
            import time
            while True:
                print(".", flush=True)
                time.sleep(0.1)
        """)
        started = time.monotonic()
        with self.assertRaises(RunIncompleteError) as ctx:
            run_bounded([sys.executable, str(script)], cwd=str(self.tmp),
                        limits=RunLimits(budget=1.5, stall=1.0))
        self.assertEqual(ctx.exception.reason, "budget")
        self.assertGreaterEqual(time.monotonic() - started, 1.4)
        self.assertLess(time.monotonic() - started, 10)

    def test_budget_is_shared_across_processes_of_one_run(self) -> None:
        limits = RunLimits(budget=0.5, started=time.monotonic() - 1.0)
        marker = self.tmp / "ran.txt"
        with self.assertRaises(RunIncompleteError) as ctx:
            run_bounded([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                        cwd=str(self.tmp), limits=limits)
        self.assertEqual(ctx.exception.reason, "budget")
        self.assertIn("not launched", str(ctx.exception))
        self.assertFalse(marker.exists())

    def test_budget_replaces_the_default_timeout(self) -> None:
        """--budget may be longer than a runner's built-in timeout."""
        proc = run_bounded(
            [sys.executable, "-c", "import time; time.sleep(1.5); print('done')"],
            cwd=str(self.tmp), timeout=0.5, limits=RunLimits(budget=30),
        )
        self.assertEqual(proc.stdout.strip(), "done")

    def test_legacy_timeout_still_raises_timeout_expired_and_kills_the_tree(self) -> None:
        script, pids_file = _spawning_sleeper(self.tmp)
        with self.assertRaises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, str(script)], cwd=str(self.tmp), timeout=1.5)
        _wait_for_file(pids_file)
        self.assertTrue(_wait_all_dead(json.loads(pids_file.read_text(encoding="utf-8"))))

    @unittest.skipUnless(os.name == "nt", "Windows Job Object behaviour")
    def test_job_object_holds_the_tree_from_the_first_instruction(self) -> None:
        """The job alone (no taskkill) must reach a grandchild.

        Assigning the job after a normal start raced the child: a venv
        `python.exe` launcher or a `.cmd` shim spawns its real child within
        milliseconds, outside the job. The process is now created suspended.
        """
        code = (
            "import subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        proc, job = bounded._spawn(
            [sys.executable, "-c", code],
            {"stdout": subprocess.PIPE, "stdin": subprocess.DEVNULL, "cwd": str(self.tmp)},
        )
        self.addCleanup(lambda: bounded._ProcessTree(proc, job).kill())
        self.assertIsNotNone(job)
        grandchild = int(proc.stdout.readline())
        job.terminate()
        proc.wait(timeout=5)
        proc.stdout.close()
        self.assertTrue(_wait_all_dead([proc.pid, grandchild]))

    def test_completed_run_returns_output_and_exit_code(self) -> None:
        proc = run_bounded(
            [sys.executable, "-c", "import sys; print('ok'); sys.exit(2)"],
            cwd=str(self.tmp), limits=RunLimits(budget=30, stall=10),
        )
        self.assertEqual((proc.returncode, proc.stdout.strip()), (2, "ok"))


class CliBudgetStallTests(unittest.TestCase):
    """End to end through `waterfree testing run`, using the unittest runner."""

    def setUp(self) -> None:
        self.workspace = make_temp_dir(self, prefix="cli-stall-")
        self.tests = self.workspace / "tests"
        self.tests.mkdir()
        self.pid_file = self.workspace / "test_pid.txt"

    def _hanging_test(self, *, print_parse_error: bool) -> None:
        emit = f"sys.stderr.write({PARSE_ERROR!r}); sys.stderr.flush()" if print_parse_error else ""
        (self.tests / "test_hang.py").write_text(textwrap.dedent(f"""
            import os, sys, time, unittest

            class HangTests(unittest.TestCase):
                def test_hangs(self):
                    with open({str(self.pid_file)!r}, "w") as handle:
                        handle.write(str(os.getpid()))
                    {emit}
                    time.sleep(60)
        """), encoding="utf-8")

    def _run(self, *extra: str) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch(["testing", "run", "--workspace", str(self.workspace),
                             "--runner", "unittest", *extra])
        return code, json.loads(out.getvalue())

    def test_silent_hang_is_killed_and_reported_incomplete(self) -> None:
        self._hanging_test(print_parse_error=False)
        code, payload = self._run("--stall", "2s")
        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertTrue(payload["incomplete"])
        self.assertEqual(payload["reason"], "stall")
        self.assertEqual(payload["passed"], 0)
        self.assertIn("elapsedSeconds", payload)
        self.assertEqual(payload["limitSeconds"], 2.0)
        _wait_for_file(self.pid_file)
        self.assertTrue(_wait_all_dead([int(self.pid_file.read_text(encoding="utf-8"))]))

    def test_parse_error_then_hang_names_the_error(self) -> None:
        self._hanging_test(print_parse_error=True)
        code, payload = self._run("--stall", "2s", "--budget", "60s")
        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertEqual(payload["reason"], "stall")
        self.assertIn("Parse Error", payload["firstError"])
        self.assertEqual(payload["firstErrorLocation"], "res://scripts/player.gd:12")
        _wait_for_file(self.pid_file)
        self.assertTrue(_wait_all_dead([int(self.pid_file.read_text(encoding="utf-8"))]))
        # The partial output is kept for `testing logs`.
        log = (self.workspace / ".waterfree" / "testing" / "last_run.log").read_text(encoding="utf-8")
        self.assertIn("Parse Error", log)

    def test_invalid_duration_is_a_usage_error(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            dispatch(["testing", "run", "--workspace", str(self.workspace), "--stall", "soon"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
