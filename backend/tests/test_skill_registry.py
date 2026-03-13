import unittest
from pathlib import Path

from backend.llm.skills.registry import SkillRegistry
from backend.test_support import make_temp_dir as make_test_dir


class SkillRegistryTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        workspace = make_test_dir(self, prefix="skill-registry-")
        (workspace / "skills" / "demo-skill").mkdir(parents=True, exist_ok=False)
        return workspace

    def test_registry_discovers_skill_and_detail(self) -> None:
        workspace = self.make_workspace()
        skill_md = workspace / "skills" / "demo-skill" / "SKILL.md"
        skill_md.write_text(
            "---\n"
            "name: demo-skill\n"
            "description: Demo skill for testing.\n"
            "---\n\n"
            "# Demo Skill\n"
            "Use this for demo work.\n",
            encoding="utf-8",
        )

        registry = SkillRegistry(str(workspace))
        skills = registry.list_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].id, "demo-skill")

        detail = registry.get_skill_detail("demo-skill")
        self.assertIn("Demo Skill", detail["markdown"])
        self.assertEqual(detail["references"], [])
        self.assertEqual(detail["scripts"], [])


if __name__ == "__main__":
    unittest.main()
