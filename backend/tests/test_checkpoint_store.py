import unittest
from pathlib import Path

from backend.llm.checkpoints.store import CheckpointStore
from backend.test_support import make_temp_dir as make_test_dir
from backend.handlers.runtime_handler import handle_record_acp_delegation, handle_list_checkpoints, handle_resume_checkpoint
from unittest import mock


class CheckpointStoreTests(unittest.TestCase):
    def test_delegation_audit_survives_reopening_and_cannot_resume(self) -> None:
        workspace = str(self.make_workspace())
        started = handle_record_acp_delegation(None, {
            "workspacePath": workspace, "state": "started", "agentId": "hermes",
            "taskId": "task-1", "prompt": "fix parser", "model": "vendor/model",
        })["checkpoint"]
        self.assertEqual(started["status"], "running")
        self.assertFalse(started["resumable"])
        self.assertFalse(started["requiresApproval"])
        server = mock.Mock()
        server._get_runtime.return_value.list_checkpoints.return_value = []
        listed = handle_list_checkpoints(server, {"workspacePath": workspace})["checkpoints"]
        self.assertEqual(listed[0]["payload"]["prompt"], "fix parser")
        self.assertEqual(listed[0]["subagentId"], "hermes")
        with self.assertRaisesRegex(ValueError, "cannot be resumed"):
            handle_resume_checkpoint(server, {"workspacePath": workspace, "checkpointId": started["id"]})
        finished = handle_record_acp_delegation(None, {
            "workspacePath": workspace, "state": "finished", "checkpointId": started["id"],
            "result": {"ok": True, "sessionId": "agent-session", "touchedFiles": ["parser.py"], "attempts": 1},
        })["checkpoint"]
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["touchedFiles"], ["parser.py"])
        store = CheckpointStore(workspace)
        self.addCleanup(store.close)
        self.assertEqual(store.get_checkpoint(started["id"])["payload"]["result"]["sessionId"], "agent-session")
        with self.assertRaisesRegex(ValueError, "already finalized"):
            store.finish_delegation(started["id"], {"ok": False})

    def test_failed_delegation_retains_failure_and_touched_files(self) -> None:
        workspace = str(self.make_workspace())
        started = handle_record_acp_delegation(None, {
            "workspacePath": workspace, "state": "started", "agentId": "hermes", "prompt": "fix",
        })["checkpoint"]
        result = handle_record_acp_delegation(None, {
            "workspacePath": workspace, "state": "finished", "checkpointId": started["id"],
            "result": {"ok": False, "failureReason": "process died", "touchedFiles": ["partial.py"]},
        })["checkpoint"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["payload"]["result"]["failureReason"], "process died")
        self.assertEqual(result["touchedFiles"], ["partial.py"])

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
