import unittest

from backend.llm.providers.wizard_stage_runner import WizardStageRunner


class _NullExecutor:
    def _run_deepagents_structured(self, **kwargs):
        return None


class _StubSkillAdapter:
    def select(self, **kwargs):
        return None

    def augment_context(self, context, bundle):
        return context


class WizardStageRunnerTests(unittest.TestCase):
    def test_coding_stage_fallback_emits_implementation_backlog_stack(self) -> None:
        runner = WizardStageRunner(
            executor=_NullExecutor(),
            skill_adapter=_StubSkillAdapter(),
        )

        payload = runner.run_wizard_stage(
            stage_kind="coding_agents",
            stage_title="Coding Agents",
            goal="Neighborhood flood alert app",
            context="Accepted architecture and BDD notes.",
            chunk_specs=[{"id": "build_tasks", "title": "Build Tasks"}],
            persona="coding_agent",
        )

        titles = {todo["title"] for todo in payload["todos"]}
        task_types = {todo["taskType"] for todo in payload["todos"]}

        self.assertGreaterEqual(len(payload["todos"]), 4)
        self.assertIn("Validate implementation contracts", titles)
        self.assertIn("Implement core procedures for Neighborhood flood alert app", titles)
        self.assertIn("review", task_types)
        self.assertIn("impl", task_types)
        self.assertIn("refactor", task_types)
        self.assertIn("test", task_types)


if __name__ == "__main__":
    unittest.main()
