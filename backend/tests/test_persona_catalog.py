import json
import os
import unittest

import backend.llm.personas.registry as persona_registry
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

    def test_reload_personas_seeds_initial_personas_defaults(self) -> None:
        personas = reload_personas(force_seed=True)

        self.assertIn("architect", personas)
        planning = build_system_prompt("PLANNING", "architect")
        self.assertIn("Translate the user's business goal into explicit technical requirements.", planning)

    def test_design_auditor_is_seeded_with_its_audit_stage_fragment(self) -> None:
        personas = reload_personas(force_seed=True)

        self.assertIn("design_auditor", personas)
        prompt = build_system_prompt("DESIGN_AUDIT", "design_auditor")
        self.assertIn("Design Auditor", prompt)
        self.assertIn("Audit Mode", prompt)

    def test_new_bundled_persona_reaches_an_existing_catalog(self) -> None:
        """
        A persona bundled after a user's first run must still arrive. The old
        empty-catalog gate skipped seeding entirely once any persona existed,
        so additions shipped to nobody with an established install.
        """
        reload_personas(force_seed=True)
        root = persona_catalog_root()

        # Simulate an install predating design_auditor: remove it and drop its
        # id from the marker, leaving the rest of the catalog populated.
        seeded_path = root / persona_registry._SEEDED_MARKER_FILENAME
        data = json.loads(seeded_path.read_text(encoding="utf-8"))
        data["seeded"] = [pid for pid in data["seeded"] if pid != "design_auditor"]
        seeded_path.write_text(json.dumps(data), encoding="utf-8")
        for child in (root / "design_auditor").iterdir():
            child.unlink()
        (root / "design_auditor").rmdir()

        personas = reload_personas()

        self.assertIn("design_auditor", personas)
        self.assertIn("design_auditor", json.loads(seeded_path.read_text(encoding="utf-8"))["seeded"])

    def test_deleted_persona_is_not_resurrected(self) -> None:
        """Seeding must respect a deliberate deletion, or the fix trades one bug for another."""
        reload_personas(force_seed=True)
        root = persona_catalog_root()

        target = root / "socratic"
        self.assertTrue(target.exists(), "precondition: socratic ships bundled")
        for child in target.iterdir():
            child.unlink()
        target.rmdir()

        personas = reload_personas()

        self.assertNotIn("socratic", personas)
        self.assertFalse(target.exists())

    def test_marker_migration_reseeds_once_then_respects_deletion(self) -> None:
        """
        A catalog predating the marker cannot distinguish "newly bundled" from
        "deliberately deleted" — both are just absent. The migration run re-seeds
        and records the marker; the deletion sticks from then on.
        """
        reload_personas(force_seed=True)
        root = persona_catalog_root()

        def remove(persona_id: str) -> None:
            target = root / persona_id
            for child in target.iterdir():
                child.unlink()
            target.rmdir()

        # Pre-marker state: no marker, one persona absent.
        (root / persona_registry._SEEDED_MARKER_FILENAME).unlink()
        remove("socratic")

        migrated = reload_personas()
        self.assertIn("socratic", migrated, "migration run re-seeds what it cannot classify")
        self.assertTrue((root / persona_registry._SEEDED_MARKER_FILENAME).exists())

        # Now the marker exists, so a second deletion is permanent.
        remove("socratic")
        self.assertNotIn("socratic", reload_personas())
        self.assertNotIn("socratic", reload_personas())

    def test_registry_points_to_initial_personas_seed_source(self) -> None:
        self.assertEqual(persona_registry._INITIAL_PERSONAS_ROOT.name, "initial_personas")
        self.assertTrue((persona_registry._INITIAL_PERSONAS_ROOT / "architect" / "SKILL.md").exists())

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
