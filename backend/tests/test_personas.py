import unittest

from backend.llm.personas import PERSONAS
from backend.llm.prompt_templates import build_system_prompt


class PersonaPromptTests(unittest.TestCase):
    def test_architect_planning_emphasizes_research_risk_and_handoff(self) -> None:
        planning = build_system_prompt("PLANNING", "architect")
        question_answer = build_system_prompt("QUESTION_ANSWER", "architect")

        self.assertIn("Translate the user's business goal into explicit technical requirements.", planning)
        self.assertIn("State feasibility, constraints, and confidence level", planning)
        self.assertIn("Prefer research-first planning.", planning)
        self.assertIn("framework and similar-project comparison", planning)
        self.assertIn("fall back to local architecture, docs, and knowledge.", planning)
        self.assertIn("Use backlog tasks to capture policy work", planning)
        self.assertIn("Offer concrete options, not generic reassurance.", question_answer)
        self.assertIn("Push back when the current idea is underspecified", question_answer)

    def test_pattern_expert_planning_emphasizes_framework_fit_and_policy(self) -> None:
        planning = build_system_prompt("PLANNING", "pattern_expert")
        question_answer = build_system_prompt("QUESTION_ANSWER", "pattern_expert")

        self.assertIn("Evaluate framework and library fit", planning)
        self.assertIn("Build concrete technical policies", planning)
        self.assertIn("Emit backlog tasks for pattern policy work", planning)
        self.assertIn("Offer alternatives with trade-offs", question_answer)
        self.assertIn("future rewrites or coupling", question_answer)

    def test_stub_wireframer_is_registered(self) -> None:
        persona = PERSONAS["stub_wireframer"]

        self.assertEqual(persona.name, "Stub/Wireframes")
        self.assertEqual(persona.icon, "Stub")

    def test_stub_wireframer_has_stage_specific_prompting(self) -> None:
        planning = build_system_prompt("PLANNING", "stub_wireframer")
        execution = build_system_prompt("EXECUTION", "stub_wireframer")
        architect = build_system_prompt("PLANNING", "architect")

        self.assertIn("subsystem-sized roughing tasks", planning)
        self.assertIn("TODO: [wf]", execution)
        self.assertNotIn("subsystem-sized roughing tasks", architect)


if __name__ == "__main__":
    unittest.main()
