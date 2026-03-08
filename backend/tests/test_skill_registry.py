import shutil
import unittest
import uuid
from pathlib import Path

from backend.llm.skills.registry import SkillRegistry

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_skill_registry_tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class SkillRegistryTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        workspace = _TMP_ROOT / uuid.uuid4().hex
        (workspace / "skills" / "demo-skill").mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
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
