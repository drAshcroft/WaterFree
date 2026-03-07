import shutil
import sys
import types
import unittest
import uuid
from pathlib import Path

if "anthropic" not in sys.modules:
    sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=object, types=types.SimpleNamespace(Message=object))

from backend.llm.claude_client import ClaudeClient
from backend.todo.store import TaskStore

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_claude_service_tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class FakeKnowledgeEntry:
    def __init__(self, title: str):
        self._title = title

    def to_dict(self) -> dict:
        return {"title": self._title}


class FakeKnowledgeRepo:
    def __init__(self, name: str):
        self._name = name

    def to_dict(self) -> dict:
        return {"name": self._name}


class FakeKnowledgeStore:
    def search(self, query: str, limit: int = 10):
        return [FakeKnowledgeEntry(f"match:{query}")][:limit]

    def list_repos(self):
        return [FakeKnowledgeRepo("demo-repo")]

    def total_entries(self) -> int:
        return 1


class ClaudeClientServiceToolTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        workspace = _TMP_ROOT / uuid.uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        return workspace

    def make_client(self) -> ClaudeClient:
        client = ClaudeClient.__new__(ClaudeClient)
        client._client = None
        client._graph = None
        client._knowledge_store = FakeKnowledgeStore()
        client._task_store_factory = lambda workspace_path: TaskStore(workspace_path)
        client._task_stores = {}
        return client

    def test_task_tools_add_and_fetch_ready_work(self) -> None:
        workspace = self.make_workspace()
        client = self.make_client()

        added = client._execute_host_tool(
            "add_task",
            {
                "workspacePath": str(workspace),
                "title": "Fix login bug",
                "priority": "P1",
                "owner": {"type": "agent", "name": "codex"},
                "targetCoord": {"file": "src/auth.py", "line": 41, "anchorType": "modify"},
            },
            str(workspace),
        )
        listed = client._execute_host_tool(
            "list_tasks",
            {"workspacePath": str(workspace), "readyOnly": True},
            str(workspace),
        )
        next_task = client._execute_host_tool(
            "what_next",
            {"workspacePath": str(workspace), "ownerName": "codex"},
            str(workspace),
        )

        self.assertEqual(added["task"]["title"], "Fix login bug")
        self.assertEqual(len(listed["tasks"]), 1)
        self.assertEqual(next_task["task"]["title"], "Fix login bug")

    def test_knowledge_tools_are_available(self) -> None:
        workspace = self.make_workspace()
        client = self.make_client()

        result = client._execute_host_tool(
            "search_knowledge",
            {"query": "auth"},
            str(workspace),
        )
        sources = client._execute_host_tool(
            "list_knowledge_sources",
            {},
            str(workspace),
        )

        self.assertEqual(result["entries"][0]["title"], "match:auth")
        self.assertEqual(sources["repos"][0]["name"], "demo-repo")


if __name__ == "__main__":
    unittest.main()
