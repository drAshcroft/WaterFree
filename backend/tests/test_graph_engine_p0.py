import os
import shutil
import textwrap
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.graph.engine import GraphEngine, _project_name

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_graph_tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class GraphEngineP0Tests(unittest.TestCase):
    def make_temp_root(self) -> Path:
        root = _TMP_ROOT / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def make_repo(self, root: Path, relative_path: str, files: dict[str, str]) -> Path:
        repo = root / relative_path
        repo.mkdir(parents=True, exist_ok=True)
        for rel_path, content in files.items():
            file_path = repo / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")
        return repo

    def test_restart_persistence_discovers_multiple_graph_dbs(self) -> None:
        root = self.make_temp_root()
        repo_one = self.make_repo(
            root,
            "group_one/app",
            {"main.py": "def alpha():\n    return 1\n"},
        )
        repo_two = self.make_repo(
            root,
            "group_two/app",
            {"main.py": "def beta():\n    return 2\n"},
        )

        engine = GraphEngine()
        try:
            first = engine.index_repository(str(repo_one))
            second = engine.index_repository(str(repo_two))
        finally:
            engine.close()

        self.assertNotEqual(first["project"], second["project"])
        self.assertEqual(first["project"], _project_name(str(repo_one.resolve())))
        self.assertEqual(second["project"], _project_name(str(repo_two.resolve())))

        with working_directory(root):
            restarted = GraphEngine()
            self.addCleanup(restarted.close)
            projects = restarted.list_projects()["projects"]

            discovered = {(item["root_path"], item["name"]) for item in projects}
            self.assertEqual(
                discovered,
                {
                    (str(repo_one.resolve()), first["project"]),
                    (str(repo_two.resolve()), second["project"]),
                },
            )
            self.assertTrue(all(Path(item["db_path"]).is_file() for item in projects))

    def test_index_status_survives_restart(self) -> None:
        root = self.make_temp_root()
        repo = self.make_repo(
            root,
            "status_repo",
            {"service.py": "def run():\n    return 'ok'\n"},
        )

        engine = GraphEngine()
        self.addCleanup(engine.close)
        status_before = engine.index_status(repo_path=str(repo))
        self.assertEqual(status_before["status"], "not_indexed")

        indexed = engine.index_repository(str(repo))
        project = indexed["project"]
        engine.close()

        with working_directory(root):
            restarted = GraphEngine()
            self.addCleanup(restarted.close)
            status_after = restarted.index_status(repo_path=str(repo))

        self.assertEqual(status_after["status"], "ready")
        self.assertEqual(status_after["project"], project)
        self.assertEqual(status_after["root_path"], str(repo.resolve()))
        self.assertTrue(Path(status_after["db_path"]).is_file())

    def test_deleted_file_cleanup_removes_nodes_edges_and_hashes(self) -> None:
        root = self.make_temp_root()
        repo = self.make_repo(
            root,
            "cleanup_repo",
            {
                "a.py": """
                def foo():
                    return 1
                """,
                "b.py": """
                from a import foo

                def caller():
                    return foo()
                """,
            },
        )

        engine = GraphEngine()
        self.addCleanup(engine.close)
        initial = engine.index_repository(str(repo))
        project = initial["project"]
        store = engine._store(project)

        caller = store.get_node_by_qn(project, f"{project}.b.caller")
        self.assertIsNotNone(caller)
        self.assertEqual(len(store.get_outbound_edges(project, caller["id"], "CALLS")), 1)
        self.assertIn("a.py", store.get_file_hashes(project))

        (repo / "a.py").unlink()

        result = engine.index_repository(str(repo))
        caller = store.get_node_by_qn(project, f"{project}.b.caller")

        self.assertEqual(result["deleted_files"], 1)
        self.assertEqual(result["status"], "up_to_date")
        self.assertNotIn("a.py", store.get_file_hashes(project))
        self.assertIsNone(store.get_node_by_qn(project, f"{project}.a.foo"))
        self.assertEqual(store.get_outbound_edges(project, caller["id"], "CALLS"), [])
        self.assertFalse(
            any(node["file_path"] == str((repo / "a.py").resolve()) for node in store.get_all_nodes(project))
        )


if __name__ == "__main__":
    unittest.main()
