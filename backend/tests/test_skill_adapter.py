import shutil
import unittest
import uuid
from pathlib import Path

from backend.llm.skills import SkillAdapter, SkillRegistry

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_skill_adapter_tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class SkillAdapterTests(unittest.TestCase):
    def make_workspace(self) -> Path:
        workspace = _TMP_ROOT / uuid.uuid4().hex
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        return workspace

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
