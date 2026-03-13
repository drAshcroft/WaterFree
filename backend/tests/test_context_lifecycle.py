import json
import unittest
from pathlib import Path

from backend.llm.context_lifecycle import ContextLifecycleManager
from backend.test_support import make_temp_dir as make_test_dir


class ContextLifecycleTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="context-lifecycle-")

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
