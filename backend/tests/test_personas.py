import unittest

from backend.llm.personas import PERSONAS
from backend.llm.prompt_templates import build_system_prompt


class PersonaPromptTests(unittest.TestCase):
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
