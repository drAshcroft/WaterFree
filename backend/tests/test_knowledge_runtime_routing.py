import unittest
from pathlib import Path

from backend.knowledge.extractor import KnowledgeExtractor
from backend.knowledge.procedure_extractor import extract_procedure
from backend.knowledge.store import KnowledgeStore
from backend.test_support import make_temp_dir as make_test_dir


class FakeKnowledgeRuntime:
    def __init__(self) -> None:
        self.triage_calls = 0
        self.describe_calls = 0
        self.procedure_calls = 0

    def triage_knowledge_symbols(self, *, source_repo: str, focus: str, batch: list[dict], workspace_path: str = "") -> list[int]:
        self.triage_calls += 1
        return [0]

    def describe_knowledge_batch(self, *, source_repo: str, focus: str, batch: list[dict], workspace_path: str = "") -> list[dict]:
        self.describe_calls += 1
        return [
            {
                "index": 0,
                "keep": True,
                "snippet_type": "pattern",
                "title": "Reusable auth guard",
                "description": "Guards requests before reaching the handler.",
                "tags": ["python", "auth"],
            }
        ]

    def summarize_procedure_knowledge(self, *, context: str, focus: str = "", workspace_path: str = "") -> dict:
        self.procedure_calls += 1
        return {
            "keep": True,
            "snippet_type": "pattern",
            "title": "Procedure summary",
            "description": "Summarizes a root procedure and its call chain.",
            "tags": ["python", "workflow"],
        }


class FakeGraph:
    def find_qualified_name(self, name: str) -> str:
        return "demo.auth.login"

    def get_code_snippet(self, qname: str, auto_resolve: bool = True) -> dict:
        if qname == "demo.auth.login":
            return {
                "source": "def login(user):\n    return normalize(user)\n",
                "file_path": "src/auth.py",
                "signature": "login(user)",
            }
        if qname == "normalize":
            return {
                "source": "def normalize(user):\n    return user.strip()\n",
                "file_path": "src/auth.py",
                "signature": "normalize(user)",
            }
        return {}

    def trace_call_path(self, name: str, direction: str = "outbound", depth: int = 3, risk_labels: bool = False) -> dict:
        return {
            "nodes": [
                {"name": "login", "qualified_name": "demo.auth.login", "depth": 0, "file_path": "src/auth.py"},
                {"name": "normalize", "qualified_name": "normalize", "depth": 1, "file_path": "src/auth.py"},
            ]
        }

    def search_graph(self, **kwargs) -> dict:
        return {"nodes": []}


class KnowledgeRuntimeRoutingTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="knowledge-runtime-")

    def make_store(self, workspace: Path) -> KnowledgeStore:
        db_path = workspace / "knowledge.db"
        store = KnowledgeStore(str(db_path))
        self.addCleanup(store.close)
        return store

    def test_extractor_uses_runtime_for_triage_and_description(self) -> None:
        workspace = self.make_workspace()
        store = self.make_store(workspace)
        runtime = FakeKnowledgeRuntime()
        extractor = KnowledgeExtractor(
            store=store,
            runtime=runtime,
            source_repo="demo-repo",
            focus="auth",
            workspace_path=str(workspace),
        )

        added = extractor.extract_from_symbols([
            {"name": "login", "label": "function", "file_path": "src/auth.py", "body": "def login(user):\n    return normalize(user)\n"},
            {"name": "tiny", "label": "function", "file_path": "src/auth.py", "body": "x"},
        ])

        self.assertEqual(added, 1)
        self.assertEqual(runtime.triage_calls, 1)
        self.assertEqual(runtime.describe_calls, 1)
        self.assertEqual(store.total_entries(), 1)

    def test_extract_procedure_uses_runtime_summary_lane(self) -> None:
        workspace = self.make_workspace()
        store = self.make_store(workspace)
        runtime = FakeKnowledgeRuntime()

        result = extract_procedure(
            graph=FakeGraph(),
            store=store,
            runtime=runtime,
            name="login",
            source_repo="demo-repo",
            focus="auth",
            workspace_path=str(workspace),
            max_depth=2,
        )

        self.assertTrue(result["kept"])
        self.assertEqual(runtime.procedure_calls, 1)
        self.assertEqual(store.total_entries(), 1)


if __name__ == "__main__":
    unittest.main()
