import json
import shutil
import unittest
import uuid
from pathlib import Path

from backend.llm.context_lifecycle import ContextLifecycleManager

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_context_lifecycle_tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class ContextLifecycleTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        workspace = _TMP_ROOT / uuid.uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        return workspace

    def test_low_value_chunk_is_compressed_after_two_turns(self) -> None:
        workspace = self.make_workspace()
        manager = ContextLifecycleManager()
        long_irrelevant = "infra notes " * 40
        raw = (
            "SESSION GOAL: fix auth pipeline and login flow\n\n"
            f"BACKGROUND: {long_irrelevant}\n\n"
            "CURRENT TASK: update auth token refresh logic"
        )

        first = manager.govern(
            workspace_path=str(workspace),
            session_id="s1",
            stage="planning",
            query="auth login token refresh",
            raw_context=raw,
            budget_chars=20000,
        )
        second = manager.govern(
            workspace_path=str(workspace),
            session_id="s1",
            stage="planning",
            query="auth login token refresh",
            raw_context=raw,
            budget_chars=20000,
        )

        self.assertEqual(first.stats["chunks"], 3)
        self.assertGreaterEqual(second.stats["compressed"], 1)
        self.assertIn("[COMPRESSED CONTEXT]", second.context)

    def test_reset_can_remove_one_session(self) -> None:
        workspace = self.make_workspace()
        manager = ContextLifecycleManager()
        raw = "A\n\nB"
        manager.govern(
            workspace_path=str(workspace),
            session_id="s1",
            stage="question",
            query="a",
            raw_context=raw,
        )
        manager.govern(
            workspace_path=str(workspace),
            session_id="s2",
            stage="question",
            query="b",
            raw_context=raw,
        )

        result = manager.reset(str(workspace), session_id="s1")
        self.assertTrue(result["ok"])

        state_path = workspace / ".waterfree" / "context_lifecycle.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("s1", data.get("sessions", {}))
        self.assertIn("s2", data.get("sessions", {}))


if __name__ == "__main__":
    unittest.main()
