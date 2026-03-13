import sys
import types
import unittest
from pathlib import Path

if "anthropic" not in sys.modules:
    sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=object, types=types.SimpleNamespace(Message=object))

from backend.server import Server
from backend.session.models import AIState, PlanDocument, Task, TaskStatus
from backend.test_support import make_temp_dir as make_test_dir


class ServerFinalizeExecutionTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="server-finalize-")

    def make_server(self) -> Server:
        server = Server.__new__(Server)
        server._graph = None
        server._sessions = {}
        server._session_managers = {}
        server._index_state_stores = {}
        server._task_stores = {}
        server._runtime = None
        server._knowledge_store = None
        server._context_lifecycle = None
        return server

    def make_doc(self, workspace: Path) -> PlanDocument:
        task = Task(
            id="task-1",
            title="Rough auth shell",
            description="Create auth subsystem skeletons",
            status=TaskStatus.EXECUTING,
        )
        return PlanDocument(
            id="session-1",
            goal_statement="Rough the auth subsystem",
            workspace_path=str(workspace),
            tasks=[task],
            ai_state=AIState.SCANNING,
        )

    def test_finalize_execution_completes_clean_task(self) -> None:
        workspace = self.make_workspace()
        server = self.make_server()
        doc = self.make_doc(workspace)
        server._sessions[doc.id] = doc

        result = server.handle_finalize_execution({
            "sessionId": doc.id,
            "taskId": "task-1",
            "diagnostics": [],
        })

        self.assertTrue(result["ok"])
        self.assertEqual(doc.tasks[0].status, TaskStatus.COMPLETE)
        self.assertEqual(doc.ai_state, AIState.IDLE)
        self.assertIsNotNone(doc.tasks[0].completed_at)
        self.assertIsNone(doc.tasks[0].blocked_reason)

    def test_finalize_execution_reopens_task_on_blocking_diagnostics(self) -> None:
        workspace = self.make_workspace()
        server = self.make_server()
        doc = self.make_doc(workspace)
        server._sessions[doc.id] = doc

        result = server.handle_finalize_execution({
            "sessionId": doc.id,
            "taskId": "task-1",
            "diagnostics": [
                {
                    "file": "src/auth.ts",
                    "line": 14,
                    "severity": "error",
                    "source": "tsserver",
                    "message": "Cannot find name 'AuthShell'.",
                }
            ],
        })

        self.assertFalse(result["ok"])
        self.assertEqual(doc.tasks[0].status, TaskStatus.NEGOTIATING)
        self.assertEqual(doc.ai_state, AIState.AWAITING_REVIEW)
        self.assertIn("Cannot find name 'AuthShell'.", doc.tasks[0].blocked_reason or "")


if __name__ == "__main__":
    unittest.main()
