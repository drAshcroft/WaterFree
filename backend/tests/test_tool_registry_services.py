import shutil
import unittest
import uuid
from pathlib import Path

from backend.llm.tools.registry import build_default_tool_registry
from backend.todo.store import TaskStore

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_tool_registry_service_tests"
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


class ToolRegistryServiceTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        workspace = _TMP_ROOT / uuid.uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        return workspace

    def make_registry(self):
        return build_default_tool_registry(
            graph=None,
            task_store_factory=lambda workspace_path: TaskStore(workspace_path),
            knowledge_store_factory=FakeKnowledgeStore,
            enable_optional_web_tools=False,
        )

    def test_task_tools_add_and_fetch_ready_work(self) -> None:
        workspace = self.make_workspace()
        registry = self.make_registry()

        added = registry.invoke(
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
        listed = registry.invoke(
            "list_tasks",
            {"workspacePath": str(workspace), "readyOnly": True},
            str(workspace),
        )
        next_task = registry.invoke(
            "what_next",
            {"workspacePath": str(workspace), "ownerName": "codex"},
            str(workspace),
        )

        self.assertEqual(added["task"]["title"], "Fix login bug")
        self.assertEqual(len(listed["tasks"]), 1)
        self.assertEqual(next_task["task"]["title"], "Fix login bug")

    def test_knowledge_tools_are_available(self) -> None:
        workspace = self.make_workspace()
        registry = self.make_registry()

        result = registry.invoke(
            "search_knowledge",
            {"query": "auth"},
            str(workspace),
        )
        sources = registry.invoke(
            "list_knowledge_sources",
            {},
            str(workspace),
        )

        self.assertEqual(result["entries"][0]["title"], "match:auth")
        self.assertEqual(sources["repos"][0]["name"], "demo-repo")


if __name__ == "__main__":
    unittest.main()
