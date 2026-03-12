import shutil
import unittest
import uuid
from pathlib import Path

from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.store import KnowledgeStore

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_knowledge_hierarchy_tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class KnowledgeHierarchyTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        workspace = _TMP_ROOT / uuid.uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        return workspace

    def make_store(self, workspace: Path) -> KnowledgeStore:
        db_path = workspace / "knowledge.db"
        store = KnowledgeStore(str(db_path))
        self.addCleanup(store.close)
        return store

    def add_entry(
        self,
        store: KnowledgeStore,
        *,
        title: str,
        snippet_type: str = "pattern",
        tags: list[str] | None = None,
        hierarchy_path: str | None = None,
    ) -> None:
        entry = KnowledgeEntry.create(
            source_repo="demo-repo",
            source_file="src/demo.py",
            snippet_type=snippet_type,
            title=title,
            description=f"{title} description",
            code=f"def {title.replace(' ', '_')}():\n    return True\n",
            tags=tags or [],
            hierarchy_path=hierarchy_path,
        )
        store.add_entry(entry)

    def test_browse_hierarchy_uses_explicit_and_derived_paths(self) -> None:
        workspace = self.make_workspace()
        store = self.make_store(workspace)
        self.add_entry(store, title="JWT guard", hierarchy_path="platform/auth/jwt")
        self.add_entry(store, title="SQLite helper", snippet_type="utility", tags=["python", "sqlite"])

        hierarchy = store.browse_hierarchy(depth=2)

        self.assertEqual(hierarchy["entry_count"], 2)
        self.assertEqual([node["name"] for node in hierarchy["nodes"]], ["platform", "utility"])
        self.assertEqual(hierarchy["nodes"][0]["children"][0]["name"], "auth")
        self.assertEqual(hierarchy["nodes"][1]["children"][0]["name"], "python")

    def test_browse_hierarchy_filters_subtree_and_can_include_entries(self) -> None:
        workspace = self.make_workspace()
        store = self.make_store(workspace)
        self.add_entry(store, title="JWT guard", hierarchy_path="platform/auth/jwt")
        self.add_entry(store, title="Session guard", hierarchy_path="platform/auth/session")
        self.add_entry(store, title="Cache helper", hierarchy_path="platform/cache/redis")

        hierarchy = store.browse_hierarchy(
            path="platform/auth",
            depth=1,
            include_entries=True,
            entry_limit=10,
        )

        self.assertEqual(hierarchy["path"], "platform/auth")
        self.assertEqual(hierarchy["entry_count"], 2)
        self.assertEqual([node["name"] for node in hierarchy["nodes"]], ["jwt", "session"])
        self.assertEqual(len(hierarchy["entries"]), 2)


if __name__ == "__main__":
    unittest.main()
