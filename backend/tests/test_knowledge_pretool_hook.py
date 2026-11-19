"""PreToolUse heredoc-backslash warning (HOOK-HEREDOC-001)."""

import io
import json
import unittest

from backend.cli import knowledge_pretool as hook

BS = chr(92)

# The BrickWild 2026-11-14 shape: a python heredoc patching GDScript continuations.
BRICKWILD = (
    "C:/Projects/.local/Scripts/python.exe - <<'EOF'\n"
    "from pathlib import Path\n"
    "p = Path('src/house/house_plan_rooms.gd')\n"
    "s = p.read_text()\n"
    f"s = s.replace('var w := a + {BS}\\n', 'var w := a +{BS}\\n')\n"
    "p.write_text(s)\n"
    "EOF"
)


def _event(command, tool="Bash"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}}


class RiskyHeredocTests(unittest.TestCase):
    def test_brickwild_patch_warns(self) -> None:
        text = hook.evaluate(_event(BRICKWILD))
        self.assertIsNotNone(text)
        self.assertIn("Edit tool", text)
        self.assertIn("<<EOF", text)

    def test_trailing_continuation_inside_body_warns(self) -> None:
        cmd = f"cat > scripts/a.gd <<EOF\nvar x := 1 + {BS}\n    2\nEOF"
        text = hook.evaluate(_event(cmd))
        self.assertIsNotNone(text)
        self.assertIn("unquoted heredoc", text)

    def test_tab_stripped_delimiter_is_found(self) -> None:
        cmd = f"python - <<-'PY'\n\tPath('a.py').write_text('x{BS}ny')\n\tPY\necho done"
        self.assertIsNotNone(hook.evaluate(_event(cmd)))

    def test_codex_argv_payload(self) -> None:
        event = {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", BRICKWILD]}}
        self.assertIsNotNone(hook.evaluate(event))

    def test_no_backslash_in_body_is_silent(self) -> None:
        cmd = "python - <<'EOF'\nfrom pathlib import Path\nPath('a.py').write_text('x = 1')\nEOF"
        self.assertIsNone(hook.evaluate(_event(cmd)))

    def test_prose_heredoc_without_code_path_is_silent(self) -> None:
        cmd = f"cat <<'EOF'\nC:{BS}Users{BS}me is the home folder\nEOF"
        self.assertIsNone(hook.evaluate(_event(cmd)))

    def test_shell_continuation_outside_the_body_is_silent(self) -> None:
        cmd = f"python tools/run.py --flag {BS}\n  --other <<'EOF'\nprint('ok')\nEOF"
        self.assertIsNone(hook.evaluate(_event(cmd)))

    def test_other_tools_are_ignored(self) -> None:
        self.assertIsNone(hook.evaluate(_event(BRICKWILD, tool="Write")))
        self.assertIsNone(hook.evaluate({"tool_name": "Bash", "tool_input": "not a dict"}))
        self.assertIsNone(hook.evaluate(None))


class LessonCitationTests(unittest.TestCase):
    def test_cites_the_lesson_when_it_exists(self) -> None:
        text = hook.evaluate(_event(BRICKWILD), lesson_lookup=lambda _id: "Bash-tool heredocs mangle backslashes")
        self.assertIn(hook.LESSON_ID[:8], text)

    def test_store_failure_still_warns_without_citation(self) -> None:
        def boom(_id):
            raise RuntimeError("db locked")
        text = hook.evaluate(_event(BRICKWILD), lesson_lookup=boom)
        self.assertIsNotNone(text)
        self.assertNotIn(hook.LESSON_ID[:8], text)


class RunTests(unittest.TestCase):
    def _run(self, raw: str) -> str:
        out = io.StringIO()
        code, _ = hook.run(stdin=io.StringIO(raw), stdout=out)
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_match_prints_pretooluse_additional_context(self) -> None:
        payload = json.loads(self._run(json.dumps(_event(BRICKWILD))))
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", payload["hookSpecificOutput"])  # never blocks

    def test_no_match_and_malformed_input_print_nothing(self) -> None:
        self.assertEqual(self._run(json.dumps(_event("ls -la"))), "")
        self.assertEqual(self._run("{not json"), "")
        self.assertEqual(self._run(""), "")


class FastPathTests(unittest.TestCase):
    """`python -m backend.main knowledge hook-pretool` bypasses the dispatcher and logs only firings."""

    def _invoke(self, event: dict, log_path) -> "subprocess.CompletedProcess[str]":
        import os
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        env = dict(os.environ, WATERFREE_USAGE_LOG=str(log_path), PYTHONPATH=str(repo))
        return subprocess.run([sys.executable, "-m", "backend.main", "knowledge", "hook-pretool"],
                              input=json.dumps(event), capture_output=True, text=True,
                              cwd=str(repo), env=env, timeout=60)

    def test_fires_logs_once_and_skips_the_dispatcher(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "usage.jsonl"
            fired = self._invoke(_event(BRICKWILD), log)
            quiet = self._invoke(_event("git status"), log)
            records = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        self.assertEqual((fired.returncode, quiet.returncode), (0, 0))
        self.assertIn("PreToolUse", fired.stdout)
        self.assertEqual(quiet.stdout, "")
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0])["action"], "hook-pretool")


if __name__ == "__main__":
    unittest.main()
