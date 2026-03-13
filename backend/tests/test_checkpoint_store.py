import unittest
from pathlib import Path

from backend.llm.checkpoints.store import CheckpointStore
from backend.test_support import make_temp_dir as make_test_dir


class CheckpointStoreTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="checkpoint-store-")

    def test_create_resume_and_discard_checkpoint(self) -> None:
        workspace = self.make_workspace()
        store = CheckpointStore(str(workspace))
        self.addCleanup(store.close)

        checkpoint = store.create_checkpoint(
            session_id="session-1",
            reason="test",
            runtime_id="deep_agents",
            payload={"workspacePath": str(workspace), "summary": "Checkpoint summary"},
            requires_approval=True,
        )
        self.assertEqual(checkpoint["sessionId"], "session-1")
        self.assertEqual(checkpoint["status"], "pending")

        resumed = store.resume_checkpoint(checkpoint["id"], {"action": "approve"})
        self.assertEqual(resumed["status"], "approved")

        discarded = store.discard_checkpoint(checkpoint["id"])
        self.assertTrue(discarded)
        latest = store.get_checkpoint(checkpoint["id"])
        self.assertIsNotNone(latest)
        self.assertEqual(latest["status"], "discarded")


if __name__ == "__main__":
    unittest.main()
