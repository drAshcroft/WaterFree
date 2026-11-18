"""
`waterfree testing await` (TEST-AWAIT-001).

Agents waited on long headless runs with sleep/until loops, the harness blocks
plain `sleep N`, and a Monitor loop fired early on a stale log an old process
was still holding. `await` is one blocking call that ignores content older
than the call, returns on a new matching line / pid exit / timeout, and
reports only summary lines.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from backend.cli._common import EXIT_DEP_MISSING, EXIT_OK, EXIT_USAGE
from backend.cli.dispatcher import dispatch
from backend.test_support import make_temp_dir
from backend.testing.await_log import await_log

FAST = 0.05


def _append_later(path: Path, text: str, *, delay: float = 0.4, mode: str = "a") -> threading.Thread:
    def write() -> None:
        time.sleep(delay)
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(text)

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread


class AwaitLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir(self, prefix="await-")
        self.log = self.dir / "run.log"

    def test_a_line_appended_after_start_matches(self) -> None:
        self.log.write_text("Godot Engine v4.3\nRunning libraryquick...\n", encoding="utf-8")
        writer = _append_later(self.log, (
            "  libraryquick   PASS  (12 checked, 0 failures)\n"
            "noise line\n"
            "ALL PASS -- 1 suites, 12 checks, 0 failures, 0 warnings\n"
        ))
        outcome = await_log(str(self.log), r"ALL PASS|SUITE\(S\) FAILED", timeout=10, poll_interval=FAST)
        writer.join()
        self.assertTrue(outcome.matched)
        self.assertEqual(outcome.reason, "matched")
        self.assertTrue(outcome.match_line.startswith("ALL PASS"))
        self.assertLess(outcome.elapsed, 5)
        # Only summary lines, and nothing from before the call started.
        self.assertEqual(outcome.summary_lines, [
            "  libraryquick   PASS  (12 checked, 0 failures)",
            "ALL PASS -- 1 suites, 12 checks, 0 failures, 0 warnings",
        ])

    def test_a_match_in_old_content_is_ignored(self) -> None:
        self.log.write_text("ALL PASS -- stale run from yesterday\n", encoding="utf-8")
        outcome = await_log(str(self.log), "ALL PASS", timeout=0.6, poll_interval=FAST)
        self.assertFalse(outcome.matched)
        self.assertEqual(outcome.reason, "timeout")
        self.assertEqual(outcome.summary_lines, [])

    def test_timeout_is_honoured(self) -> None:
        self.log.write_text("", encoding="utf-8")
        started = time.monotonic()
        outcome = await_log(str(self.log), "never", timeout=0.8, poll_interval=FAST)
        elapsed = time.monotonic() - started
        self.assertEqual(outcome.reason, "timeout")
        self.assertGreaterEqual(elapsed, 0.75)
        self.assertLess(elapsed, 3)

    def test_pid_exit_without_a_match(self) -> None:
        self.log.write_text("", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
        self.addCleanup(proc.wait)
        outcome = await_log(str(self.log), "ALL PASS", timeout=20, pid=proc.pid, poll_interval=FAST)
        self.assertEqual(outcome.reason, "pid_exited")
        self.assertFalse(outcome.matched)
        self.assertLess(outcome.elapsed, 10)

    def test_a_final_flush_on_exit_still_matches(self) -> None:
        self.log.write_text("", encoding="utf-8")
        script = (
            "import time, sys\n"
            "time.sleep(0.3)\n"
            f"open({str(self.log)!r}, 'a').write('ALL PASS -- 3 suites')\n"  # no trailing newline
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        self.addCleanup(proc.wait)
        outcome = await_log(str(self.log), "ALL PASS", timeout=20, pid=proc.pid, poll_interval=0.5)
        self.assertTrue(outcome.matched)

    def test_a_recreated_log_is_read_from_the_top(self) -> None:
        self.log.write_text("old run\n" * 200 + "ALL PASS -- old\n", encoding="utf-8")
        writer = _append_later(self.log, "Running...\nSCRIPT ERROR: Parse Error: x\nALL PASS -- new\n", mode="w")
        outcome = await_log(str(self.log), "ALL PASS", timeout=10, poll_interval=FAST)
        writer.join()
        self.assertTrue(outcome.matched)
        self.assertEqual(outcome.match_line, "ALL PASS -- new")
        self.assertTrue(outcome.log_recreated)
        self.assertIn("SCRIPT ERROR: Parse Error: x", outcome.summary_lines)

    def test_a_log_that_appears_later_is_read(self) -> None:
        writer = _append_later(self.log, "1 passed, 0 failed\nDONE\n", mode="w")
        outcome = await_log(str(self.log), "^DONE$", timeout=10, poll_interval=FAST)
        writer.join()
        self.assertTrue(outcome.matched)
        self.assertEqual(outcome.summary_lines, ["1 passed, 0 failed"])

    def test_fresh_ignores_appends_by_an_old_writer(self) -> None:
        self.log.write_text("old process output\n", encoding="utf-8")
        writer = _append_later(self.log, "ALL PASS -- appended by the old process\n")
        outcome = await_log(str(self.log), "ALL PASS", timeout=1.0, fresh=True, poll_interval=FAST)
        writer.join()
        self.assertFalse(outcome.matched)

        writer = _append_later(self.log, "ALL PASS -- new process\n", mode="w", delay=0.2)
        outcome = await_log(str(self.log), "ALL PASS", timeout=10, fresh=True, poll_interval=FAST)
        writer.join()
        self.assertTrue(outcome.matched)

    def test_summary_lines_are_capped_keeping_first_and_last(self) -> None:
        self.log.write_text("", encoding="utf-8")
        lines = "".join(f"  FAIL check {i}\n" for i in range(500)) + "DONE\n"
        writer = _append_later(self.log, lines, delay=0.1)
        outcome = await_log(str(self.log), "^DONE$", timeout=10, max_lines=12, poll_interval=FAST)
        writer.join()
        self.assertEqual(outcome.summary_line_count, 500)
        self.assertTrue(outcome.truncated)
        self.assertEqual(len(outcome.summary_lines), 12)
        self.assertEqual(outcome.summary_lines[0], "  FAIL check 0")
        self.assertEqual(outcome.summary_lines[-1], "  FAIL check 499")

    def test_invalid_regex_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await_log(str(self.log), "(", timeout=1)


class AwaitCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir(self, prefix="await-cli-")
        self.log = self.dir / "godot.log"
        self.log.write_text("ALL PASS -- stale\n", encoding="utf-8")

    def _cli(self, *extra: str) -> tuple[int, dict | None, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch(["testing", "await", "--log", str(self.log), *extra])
        text = out.getvalue().strip()
        return code, (json.loads(text) if text else None), err.getvalue()

    def test_match_exits_0_with_compact_json(self) -> None:
        writer = _append_later(self.log, "noise\nALL PASS -- fresh\n")
        code, payload, _ = self._cli("--until", "ALL PASS", "--timeout", "10s")
        writer.join()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["reason"], "matched")
        self.assertEqual(payload["matchLine"], "ALL PASS -- fresh")
        self.assertEqual(payload["summaryLines"], ["ALL PASS -- fresh"])
        self.assertIn("elapsedSeconds", payload)

    def test_timeout_exits_4(self) -> None:
        code, payload, err = self._cli("--until", "ALL PASS", "--timeout", "500ms")
        self.assertEqual(code, EXIT_DEP_MISSING)
        self.assertEqual((payload["matched"], payload["reason"]), (False, "timeout"))
        self.assertIn("no new line matched", err)

    def test_bad_regex_is_a_usage_error(self) -> None:
        code, payload, _ = self._cli("--until", "(", "--timeout", "1s")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
