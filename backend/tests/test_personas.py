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
        self.assertIn("hand off structural decomposition", planning)
        self.assertIn("Use backlog tasks to capture policy work", planning)
        self.assertIn("Offer concrete options, not generic reassurance.", question_answer)
        self.assertIn("Push back when the current idea is underspecified", question_answer)

    def test_pattern_expert_planning_emphasizes_framework_fit_and_policy(self) -> None:
        planning = build_system_prompt("PLANNING", "pattern_expert")
        annotation = build_system_prompt("ANNOTATION", "pattern_expert")
        question_answer = build_system_prompt("QUESTION_ANSWER", "pattern_expert")

        self.assertIn("Evaluate framework and library fit", planning)
        self.assertIn("Build concrete technical policies", planning)
        self.assertIn("machine-usable design artifacts", planning)
        self.assertIn("backlog as your main product", planning)
        self.assertIn("docs/18_PATTERN_EXPERT_REFERENCE.md", planning)
        self.assertIn("route the uncertainty into a spike", planning)
        self.assertIn("Emit backlog tasks for pattern policy work", planning)
        self.assertIn("interface ownership", annotation)
        self.assertIn("public contract, block it", annotation)
        self.assertIn("Offer alternatives with trade-offs", question_answer)
        self.assertIn("method shapes", question_answer)
        self.assertIn("likely failure", question_answer)
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
        self.assertIn("doc strings", planning)
        self.assertIn("TODO: [wf]", execution)
        self.assertIn("Use available verification tools", execution)
        self.assertNotIn("subsystem-sized roughing tasks", architect)

    def test_wizard_personas_are_registered(self) -> None:
        self.assertIn("market_researcher", PERSONAS)
        self.assertIn("bdd_test_designer", PERSONAS)
        self.assertIn("coding_agent", PERSONAS)
        self.assertIn("reviewer", PERSONAS)

    def test_market_researcher_and_reviewer_have_stage_prompts(self) -> None:
        market = build_system_prompt("PLANNING", "market_researcher")
        reviewer = build_system_prompt("QUESTION_ANSWER", "reviewer")

        self.assertIn("external research", market.lower())
        self.assertIn("findings", reviewer.lower())

    def test_coding_agent_escalates_bad_guidance_and_demands_real_backlog(self) -> None:
        planning = build_system_prompt("PLANNING", "coding_agent")
        annotation = build_system_prompt("ANNOTATION", "coding_agent")
        execution = build_system_prompt("EXECUTION", "coding_agent")
        question_answer = build_system_prompt("QUESTION_ANSWER", "coding_agent")

        self.assertIn("implementation owner", planning)
        self.assertIn("real implementation backlog", planning)
        self.assertIn("files, classes, procedures", planning)
        self.assertIn("review/spike follow-ups", planning)
        self.assertIn("ask focused questions", planning)
        self.assertIn("incorrect interfaces", annotation)
        self.assertIn("Keep the human informed", execution)
        self.assertIn("another persona's output is wrong", question_answer)


if __name__ == "__main__":
    unittest.main()
