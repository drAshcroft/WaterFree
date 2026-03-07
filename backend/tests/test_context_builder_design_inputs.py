import shutil
import unittest
import uuid
from pathlib import Path

import backend.llm.context_builder as context_builder_module
from backend.llm.context_builder import ContextBuilder
from backend.session.models import PlanDocument, Task

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_context_builder_design_inputs"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class FakeGraph:
    def get_architecture(self, aspects=None) -> dict:
        return {
            "languages": [{"name": "TypeScript", "file_count": 3}],
            "entry_points": [{"name": "activate"}],
            "layers": [{"name": "ui"}, {"name": "backend"}],
            "hotspots": [],
            "clusters": [],
            "adr": {"text": "Use local files and session state as the planning source of truth."},
        }

    def search_code(self, pattern: str, regex: bool = False) -> dict:
        return {"total": 2}

    def detect_changes(self, scope: str = "unstaged", depth: int = 1) -> dict:
        return {"changed_files": []}


class ContextBuilderDesignInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_search = context_builder_module.knowledge_retriever.search_for_context
        context_builder_module.knowledge_retriever.search_for_context = lambda _query: ""

    def tearDown(self) -> None:
        context_builder_module.knowledge_retriever.search_for_context = self._orig_search

    def make_workspace(self) -> Path:
        workspace = _TMP_ROOT / uuid.uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        return workspace

    def test_planning_context_includes_ranked_design_inputs(self) -> None:
        workspace = self.make_workspace()
        (workspace / ".waterfree").mkdir(parents=True, exist_ok=True)
        (workspace / ".waterfree" / "plan.md").write_text(
            "# Rough auth\n- [ ] Build auth shell\n",
            encoding="utf-8",
        )
        (workspace / ".waterfree" / "memory.md").write_text(
            "Use a shared auth contract and token store.",
            encoding="utf-8",
        )
        docs_dir = workspace / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "01_AUTH_FLOW.md").write_text(
            "Auth flow design\nLogin issues a token\nToken refresh must reuse the auth contract.\n",
            encoding="utf-8",
        )
        (docs_dir / "02_PAYMENTS.md").write_text(
            "Payments flow design\nInvoices and reconciliation.\n",
            encoding="utf-8",
        )

        plan = PlanDocument(
            id="session-1",
            goal_statement="Rough the auth subsystem",
            workspace_path=str(workspace),
        )

        context = ContextBuilder(FakeGraph()).build_planning_context(
            "Rough the auth subsystem",
            plan,
        )

        self.assertIn("DESIGN INPUTS:", context)
        self.assertIn(".waterfree/plan.md", context)
        self.assertIn(".waterfree/memory.md", context)
        self.assertIn("docs/01_AUTH_FLOW.md", context)
        self.assertNotIn("docs/02_PAYMENTS.md", context)

    def test_annotation_context_skips_missing_design_files_cleanly(self) -> None:
        workspace = self.make_workspace()
        src_dir = workspace / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        target = src_dir / "auth.ts"
        target.write_text("export function buildAuthShell(): void {}\n", encoding="utf-8")

        plan = PlanDocument(
            id="session-2",
            goal_statement="Rough the auth subsystem",
            workspace_path=str(workspace),
        )
        task = Task(
            title="Rough auth shell",
            description="Create auth subsystem skeletons",
        )
        task.target_file = str(target)

        context = ContextBuilder(FakeGraph()).build_annotation_context(task, plan)

        self.assertIn("DESIGN INPUTS:", context)
        self.assertIn("CURRENT TASK:", context)
        self.assertNotIn(".waterfree/plan.md:", context)


if __name__ == "__main__":
    unittest.main()
