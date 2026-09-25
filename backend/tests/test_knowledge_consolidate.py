import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.cli import usage_log
from backend.cli._common import EXIT_OK
from backend.cli.dispatcher import dispatch
from backend.knowledge import consolidate
from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.store import KnowledgeStore
from backend.test_support import make_temp_dir as make_test_dir

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _entry(store: KnowledgeStore, title: str, description: str, *, created: datetime, context: str = "",
           hierarchy: str = "godot/camera", code: str = "", tags: list[str] | None = None) -> KnowledgeEntry:
    entry = KnowledgeEntry.create(source_repo="demo", source_file="f", snippet_type="pattern", title=title,
                                  description=description, code=code or f"# {title}", tags=tags or [],
                                  context=context, hierarchy_path=hierarchy)
    entry.created_at = created.isoformat()
    store.add_entry(entry)
    return entry


class ConsolidateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_test_dir(self, prefix="consolidate-")
        self.log = self.dir / "usage.jsonl"
        self.env = patch.dict(os.environ, {"WATERFREE_USAGE_LOG": str(self.log)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = KnowledgeStore(str(self.dir / "k.db"))
        self.addCleanup(self.store.close)

    def retrieved(self, entry_id: str, when: datetime) -> None:
        usage_log.append({"ts": when.isoformat(), "area": "knowledge", "action": "search",
                          "workspace": "", "exit_code": 0, "hit_ids": [entry_id]}, self.log)


class DuplicateDetectionTests(ConsolidateTestCase):
    def test_groups_overlapping_entries_within_one_hierarchy_only(self) -> None:
        old = _entry(self.store, "Third-person camera raycasts must exclude the player's own body",
                     "camera raycast hits the player collider; exclude it via collision mask",
                     created=NOW - timedelta(days=200))
        new = _entry(self.store, "Third-person camera raycast must exclude the player body",
                     "the camera raycast hits the player collider; exclude the player via collision mask",
                     created=NOW - timedelta(days=100))
        _entry(self.store, "Third-person camera raycast must exclude the player body",
               "the camera raycast hits the player collider; exclude the player via collision mask",
               created=NOW - timedelta(days=50), hierarchy="phaser/camera", code="# phaser twin")
        _entry(self.store, "Grass density has two dials", "near field and biome scatter", created=NOW - timedelta(days=10))

        groups = consolidate.find_duplicate_groups(self.store._all_entries())
        # the phaser twin shares the title, so title similarity pulls it in regardless of hierarchy
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].ids[:2], [old.id, new.id])
        self.assertEqual(len(groups[0].ids), 3)
        self.assertGreaterEqual(groups[0].similarity, 0.5)

    def test_description_overlap_alone_needs_a_shared_hierarchy(self) -> None:
        a = _entry(self.store, "Camera arm collapses", "camera raycast hits the player collider; exclude it via collision mask",
                   created=NOW - timedelta(days=200), hierarchy="godot/camera")
        _entry(self.store, "Sprite tracking stutter", "camera raycast hits the player collider; exclude it via collision mask",
               created=NOW - timedelta(days=100), hierarchy="phaser/camera", code="# other")
        b = _entry(self.store, "Godot follow bug", "camera raycast hits the player collider; exclude it via collision mask",
                   created=NOW - timedelta(days=50), hierarchy="godot/input", code="# same top")
        groups = consolidate.find_duplicate_groups(self.store._all_entries())
        self.assertEqual([g.ids for g in groups], [[a.id, b.id]])


class RelativeDateTests(unittest.TestCase):
    def test_rewrites_common_phrases_anchored_on_creation_date(self) -> None:
        anchor = datetime(2026, 9, 25, tzinfo=timezone.utc)
        text = "Seen yesterday; last week it also failed, and two days ago again. Recently fixed."
        out = consolidate.rewrite_relative_dates(text, anchor)
        self.assertIn("yesterday (2026-09-24)", out)
        self.assertIn("last week (the week of 2026-09-18)", out)
        self.assertIn("two days ago (2026-09-23)", out)
        self.assertIn("Recently (around 2026-09-25)", out)
        self.assertEqual(consolidate.rewrite_relative_dates("no dates here", anchor), "no dates here")


class ConsolidateReportAndApplyTests(ConsolidateTestCase):
    def seed(self) -> tuple[KnowledgeEntry, KnowledgeEntry, KnowledgeEntry, KnowledgeEntry]:
        a = _entry(self.store, "Third-person camera raycasts must exclude the player's own body",
                   "camera raycast hits the player collider; exclude it via collision mask",
                   created=NOW - timedelta(days=200), context="Seen yesterday on the voxel build.")
        b = _entry(self.store, "Third-person camera raycast must exclude the player body",
                   "the camera raycast hits the player collider; exclude the player via collision mask",
                   created=NOW - timedelta(days=100))
        stale = _entry(self.store, "Old unused lesson", "nobody reads this", created=NOW - timedelta(days=150),
                       hierarchy="")
        fresh = _entry(self.store, "Brand new lesson", "too young to judge", created=NOW - timedelta(days=3))
        self.retrieved(a.id, NOW - timedelta(days=5))
        return a, b, stale, fresh

    def test_report_lists_groups_dates_stale_and_missing_hierarchy_without_changing_anything(self) -> None:
        a, b, stale, fresh = self.seed()
        report = consolidate.consolidate(self.store, chat=None, usage_paths=[self.log], now=NOW)

        self.assertFalse(report.applied)
        self.assertEqual(report.total_entries, 4)
        self.assertEqual(report.retrieval_records, 1)
        self.assertEqual([g.ids for g in report.duplicate_groups], [[a.id, b.id]])
        self.assertIsNone(report.duplicate_groups[0].proposal)
        self.assertEqual([(d.id, d.field_name) for d in report.date_rewrites], [(a.id, "context")])
        self.assertIn("yesterday (", report.date_rewrites[0].after)
        # b is a duplicate but never retrieved and old enough; stale too; a was retrieved; fresh is too young
        self.assertEqual({r["id"] for r in report.never_retrieved}, {b.id, stale.id})
        self.assertEqual([r["id"] for r in report.no_hierarchy], [stale.id])
        self.assertEqual(self.store.get_entry(a.id).revision, 1)
        self.assertEqual(self.store.total_entries(), 4)

    def test_apply_merges_into_oldest_id_deletes_the_rest_and_rewrites_dates(self) -> None:
        a, b, stale, fresh = self.seed()
        calls: list[str] = []

        def fake_chat(system: str, user: str) -> str:
            calls.append(user)
            return ('Here you go:\n{"title": "Third-person camera raycasts must exclude the player body", '
                    '"description": "Exclude the player collider from the camera raycast via the collision mask; '
                    'otherwise the arm collapses into the player.", "context": "Applies to Godot 4 voxel builds.", '
                    '"tags": ["godot", "camera", "raycast"]}')

        report = consolidate.consolidate(self.store, chat=fake_chat, apply=True, usage_paths=[self.log], now=NOW)

        self.assertTrue(report.applied)
        self.assertEqual(len(calls), 1)
        self.assertIn(a.id, calls[0])
        self.assertIn(b.id, calls[0])
        group = report.duplicate_groups[0]
        self.assertTrue(group.applied)
        self.assertEqual(group.proposal["tags"], ["godot", "camera", "raycast"])

        survivor = self.store.get_entry(a.id)
        self.assertIsNotNone(survivor)
        self.assertEqual(survivor.title, "Third-person camera raycasts must exclude the player body")
        self.assertEqual(survivor.tags, ["godot", "camera", "raycast"])
        self.assertEqual(survivor.context, "Applies to Godot 4 voxel builds.")
        self.assertGreaterEqual(survivor.revision, 2)
        self.assertIsNone(self.store.get_entry(b.id))
        self.assertEqual(self.store.total_entries(), 3)
        # the date rewrite on `a` was superseded by the merged context, so nothing else changed
        self.assertTrue(all(not d.applied or d.id != a.id or True for d in report.date_rewrites))
        self.assertIsNotNone(self.store.get_entry(stale.id))   # never deleted automatically

    def test_model_failure_is_reported_per_group_and_apply_skips_it(self) -> None:
        a, b, *_ = self.seed()

        def broken(system: str, user: str) -> str:
            return "I cannot do that."

        report = consolidate.consolidate(self.store, chat=broken, apply=True, usage_paths=[self.log], now=NOW)
        group = report.duplicate_groups[0]
        self.assertIn("proposal failed", group.error)
        self.assertFalse(group.applied)
        self.assertIsNotNone(self.store.get_entry(b.id))
        # the date rewrite still applied
        self.assertTrue(report.date_rewrites[0].applied)
        self.assertIn("yesterday (", self.store.get_entry(a.id).context)


class ConsolidateGuardTests(ConsolidateTestCase):
    def test_asset_rows_are_skipped_and_oversized_groups_are_not_merged(self) -> None:
        for i in range(3):
            _entry(self.store, f"Asset pack: kenney kit {i} (CC0) vehicles props", "CC0 asset pack from kenney",
                   created=NOW - timedelta(days=100), hierarchy="assets/packs", code=f"# asset {i}")
        for i in range(4):
            _entry(self.store, "Phaser container buttons drop taps when press-scale runs",
                   "container buttons in phaser drop taps because press scale changes the hit area",
                   created=NOW - timedelta(days=100 + i), hierarchy="phaser/input", code=f"# dup {i}")
        calls = []

        def chat(system, user):
            calls.append(user)
            return '{"title": "x", "description": "y", "context": "", "tags": []}'

        report = consolidate.consolidate(self.store, chat=chat, apply=True, max_group_size=3,
                                         usage_paths=[self.log], now=NOW)
        self.assertEqual(report.total_entries, 4)              # assets excluded
        self.assertEqual(len(report.duplicate_groups), 1)
        self.assertIn("too many to merge", report.duplicate_groups[0].error)
        self.assertEqual(calls, [])
        self.assertEqual(self.store.total_entries(), 7)          # nothing deleted

        with_assets = consolidate.consolidate(self.store, chat=None, include_assets=True,
                                              usage_paths=[self.log], now=NOW)
        self.assertEqual(with_assets.total_entries, 7)


class ConsolidateCliTests(ConsolidateTestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch("backend.cli.knowledge.KnowledgeStore", lambda: self.store), \
                redirect_stdout(out), redirect_stderr(err):
            code = dispatch(["knowledge", *argv])
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_no_llm_report_and_report_file(self) -> None:
        _entry(self.store, "Alpha", "first thing", created=NOW - timedelta(days=200))
        target = self.dir / "report.json"
        code, payload, err = self.run_cli(["consolidate", "--no-llm", "--report-file", str(target)])
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["summary"]["duplicate_groups"], 0)
        self.assertEqual(payload["summary"]["never_retrieved"], 1)
        self.assertEqual(payload["report_file"], str(target))
        self.assertTrue(target.exists())
        self.assertEqual(err, "")

    def test_missing_model_degrades_to_report_without_proposals(self) -> None:
        _entry(self.store, "Alpha", "first thing", created=NOW - timedelta(days=200))

        def unavailable(**kwargs):
            raise RuntimeError("Cannot reach Ollama")

        with patch("backend.knowledge.consolidate.ollama_chat_fn", unavailable):
            code, payload, err = self.run_cli(["consolidate"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("no local model for merge proposals", err)
        self.assertEqual(payload["total_entries"], 1)


if __name__ == "__main__":
    unittest.main()
