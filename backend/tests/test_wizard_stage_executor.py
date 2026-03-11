import unittest

from backend.wizard.models import (
    WizardChunkState,
    WizardChunkStatus,
    WizardRun,
    WizardStageState,
)
from backend.wizard.stage_executor import StageExecutor


class StageExecutorTests(unittest.TestCase):
    def test_build_stage_context_includes_structured_design_artifacts(self) -> None:
        executor = StageExecutor()
        design_stage = WizardStageState(
            id="design:api-layer",
            kind="design_pattern_agent",
            title="Design Pattern Agent - API Layer",
            persona="pattern_expert",
            doc_path="design/api-layer.md",
            chunks=[
                WizardChunkState(
                    id="interfaces",
                    title="Interfaces",
                    accepted_text="Accepted interface design.",
                    status=WizardChunkStatus.ACCEPTED,
                )
            ],
            derived_artifacts={
                "designArtifacts": {
                    "subsystems": [{"name": "API Layer"}],
                    "interfaces": [{"name": "AlertGateway"}],
                }
            },
        )
        wireframe_stage = WizardStageState(
            id="wireframe:api-layer",
            kind="wireframe_agents",
            title="Wireframe Agents - API Layer",
            persona="stub_wireframer",
            doc_path="wireframes/api-layer.md",
        )
        run = WizardRun(
            id="run-1",
            wizard_id="bring_idea_to_life",
            goal="Neighborhood flood alert app",
            persona="architect",
            workspace_path="c:/repo",
            current_stage_id=wireframe_stage.id,
            stages=[design_stage, wireframe_stage],
        )

        context = executor.build_stage_context(run, wireframe_stage)

        self.assertIn("Structured Design Artifacts", context)
        self.assertIn('"AlertGateway"', context)


if __name__ == "__main__":
    unittest.main()
