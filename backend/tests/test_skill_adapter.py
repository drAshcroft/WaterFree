import unittest
from pathlib import Path

from backend.llm.skills import SkillAdapter, SkillRegistry
from backend.test_support import make_temp_dir as make_test_dir


class SkillAdapterTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        return make_test_dir(self, prefix="skill-adapter-")

    def write_skill(self, workspace: Path, name: str, description: str) -> None:
        path = workspace / "skills" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
            encoding="utf-8",
        )

    def test_pattern_expert_annotation_gets_planning_analysis_skills(self) -> None:
        workspace = self.make_workspace()
        self.write_skill(workspace, "waterfree-index", "Graph and index discovery.")
        self.write_skill(workspace, "waterfree-knowledge", "Snippet and knowledge lookup.")
        self.write_skill(workspace, "waterfree-todos", "Task and backlog management.")

        adapter = SkillAdapter(SkillRegistry(str(workspace)))
        bundle = adapter.select(persona="pattern_expert", stage="annotation")

        self.assertEqual(
            set(bundle.skill_ids),
            {"waterfree-index", "waterfree-knowledge", "waterfree-todos"},
        )
        self.assertIn("graph", bundle.preferred_tool_categories)
        self.assertIn("knowledge", bundle.preferred_tool_categories)
        self.assertIn("backlog", bundle.preferred_tool_categories)


if __name__ == "__main__":
    unittest.main()
