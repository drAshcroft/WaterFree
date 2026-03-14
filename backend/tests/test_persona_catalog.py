import json
import os
import unittest

from backend.llm.personas import persona_catalog_root, reload_personas, save_persona_documents
from backend.llm.prompt_templates import build_system_prompt
from backend.test_support import make_temp_dir as make_test_dir


class PersonaCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_appdata = os.environ.get("APPDATA")
        self._appdata_root = str(make_test_dir(self, prefix="persona-catalog-"))
        os.environ["APPDATA"] = self._appdata_root
        reload_personas(force_seed=True)

    def tearDown(self) -> None:
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._old_appdata
        reload_personas(force_seed=True)

    def test_reload_personas_seeds_bundled_defaults(self) -> None:
        personas = reload_personas(force_seed=True)

        self.assertIn("architect", personas)
        planning = build_system_prompt("PLANNING", "architect")
        self.assertIn("Translate the user's business goal into explicit technical requirements.", planning)

    def test_save_persona_documents_writes_skill_markdown_and_metadata(self) -> None:
        skill_markdown = """---
name: custom_planner
description: Custom planning persona
---

# Custom Planner

## System
You reason about build order carefully.

## Stage: PLANNING
- Prefer stable migrations over risky rewrites.
"""
        metadata = {
            "version": 1,
            "id": "custom_planner",
            "name": "Custom Planner",
            "icon": "Cust",
            "tagline": "Custom planning persona",
            "preferredModelTiers": {"PLANNING": ["balanced"]},
            "toolCategories": ["graph", "backlog"],
            "preferredSkillIds": ["waterfree-index"],
            "subagent": {"enabled": False, "description": "", "promptStage": "PLANNING"},
        }

        saved = save_persona_documents([
            {
                "personaId": "custom_planner",
                "skillMarkdown": skill_markdown,
                "metadataJson": json.dumps(metadata),
            }
        ])

        self.assertEqual(saved[0]["id"], "custom_planner")
        self.assertIn("Prefer stable migrations over risky rewrites.", build_system_prompt("PLANNING", "custom_planner"))
        persona_dir = persona_catalog_root() / "custom_planner"
        self.assertTrue((persona_dir / "SKILL.md").exists())
        self.assertTrue((persona_dir / "waterfree.persona.json").exists())


if __name__ == "__main__":
    unittest.main()
