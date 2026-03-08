import shutil
import unittest
import uuid
from pathlib import Path

from backend.session.models import CodeCoord, TaskPriority, TaskType
from backend.session.session_manager import SessionManager
from backend.todo.store import TaskStore
from backend.wizard.definitions import CODING_TEMPLATE
from backend.wizard.manager import WizardManager
from backend.wizard.models import (
    WizardStageStatus,
    WizardTodoExport,
)

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_wizard_manager_tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class _FakeRuntime:
    def run_wizard_stage(self, **kwargs) -> dict:
        stage_kind = kwargs["stage_kind"]
        stage_title = kwargs["stage_title"]
        goal = kwargs["goal"]
        chunk_specs = kwargs["chunk_specs"]

        payload = {
            "stageSummary": f"{stage_title} summary for {goal}",
            "chunks": [
                {"id": spec["id"], "content": f"{stage_title} :: {spec['title']}"}
                for spec in chunk_specs
            ],
            "todos": [],
            "subsystems": [],
            "externalResearchPrompt": "",
            "questions": [],
        }

        if stage_kind == "architect_review":
            payload["subsystems"] = ["API Layer", "Data Layer"]
        return payload


class WizardManagerTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        workspace = _TMP_ROOT / uuid.uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        return workspace

    def test_create_or_resume_run_writes_market_doc(self) -> None:
        workspace = self.make_workspace()
        manager = WizardManager(str(workspace))

        created = manager.create_or_resume_run(
            goal="Neighborhood flood alert app",
            wizard_id="bring_idea_to_life",
            persona="architect",
        )
        resumed = manager.create_or_resume_run(
            goal="Neighborhood flood alert app",
            wizard_id="bring_idea_to_life",
            persona="architect",
        )

        market_doc = workspace / ".waterfree" / "wizards" / created.id / "market-research.md"
        self.assertEqual(created.id, resumed.id)
        self.assertTrue(market_doc.exists())
        self.assertEqual(created.current_stage_id, "market_research")

    def test_accepting_architect_stage_creates_design_docs(self) -> None:
        workspace = self.make_workspace()
        manager = WizardManager(str(workspace))
        runtime = _FakeRuntime()
        run = manager.create_or_resume_run(
            goal="Neighborhood flood alert app",
            wizard_id="bring_idea_to_life",
            persona="architect",
        )

        manager.run_stage(run_id=run.id, stage_id="market_research", runtime=runtime)
        run = manager.load_run(run.id)
        for chunk in run.get_stage("market_research").chunks:
            manager.accept_chunk(run_id=run.id, stage_id="market_research", chunk_id=chunk.id)
        manager.accept_stage(run_id=run.id, stage_id="market_research")

        manager.run_stage(run_id=run.id, stage_id="architect_review", runtime=runtime)
        run = manager.load_run(run.id)
        for chunk in run.get_stage("architect_review").chunks:
            manager.accept_chunk(run_id=run.id, stage_id="architect_review", chunk_id=chunk.id)
        manager.accept_stage(run_id=run.id, stage_id="architect_review")

        refreshed = manager.load_run(run.id)
        design_ids = {stage.id for stage in refreshed.stages if stage.id.startswith("design:")}
        self.assertIn("design:api-layer", design_ids)
        self.assertIn("design:data-layer", design_ids)
        for stage_id in design_ids:
            stage = refreshed.get_stage(stage_id)
            self.assertTrue(Path(stage.doc_path).exists())

    def test_start_coding_creates_session_and_backlog_from_todos(self) -> None:
        workspace = self.make_workspace()
        manager = WizardManager(str(workspace))
        run = manager.create_or_resume_run(
            goal="Neighborhood flood alert app",
            wizard_id="bring_idea_to_life",
            persona="architect",
        )
        coding_stage = manager._ensure_static_stage(run, CODING_TEMPLATE)  # noqa: SLF001
        coding_stage.status = WizardStageStatus.ACCEPTED
        coding_stage.todo_exports = [
            WizardTodoExport(
                stage_id=coding_stage.id,
                title="Build API handlers",
                description="Implement the flood event API endpoints.",
                phase=coding_stage.title,
                priority=TaskPriority.P1,
                task_type=TaskType.IMPL,
                target_coord=CodeCoord(file="src/api/flood.ts"),
            ),
            WizardTodoExport(
                stage_id=coding_stage.id,
                title="Write BDD coverage",
                description="Add acceptance coverage for alert creation.",
                phase=coding_stage.title,
                priority=TaskPriority.P1,
                task_type=TaskType.TEST,
                target_coord=CodeCoord(file="tests/flood.test.ts"),
            ),
        ]
        manager.save_run(run)

        session_manager = SessionManager(str(workspace))
        task_store = TaskStore(str(workspace))
        sessions = {}

        result = manager.start_coding(
            run_id=run.id,
            session_manager=session_manager,
            sessions=sessions,
            task_store=task_store,
        )

        session = result["session"]
        backlog = task_store.load()
        self.assertEqual(len(session["tasks"]), 2)
        self.assertEqual(len(backlog.tasks), 2)
        self.assertTrue(result["wizard"]["linkedSessionId"])


if __name__ == "__main__":
    unittest.main()
