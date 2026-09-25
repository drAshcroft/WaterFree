import io
import json
import os
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.cli._common import EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE
from backend.cli.dispatcher import dispatch
from backend.knowledge.models import KnowledgeEntry, compute_content_hash
from backend.knowledge.store import DuplicateContentError, KnowledgeStore
from backend.test_support import make_temp_dir as make_test_dir


class KnowledgeCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = make_test_dir(self, prefix="knowledge-cli-") / "knowledge.db"
        self.stores: list[KnowledgeStore] = []

    def tearDown(self) -> None:
        for store in self.stores:
            store.close()

    def open_store(self) -> KnowledgeStore:
        store = KnowledgeStore(str(self.db_path))
        self.stores.append(store)
        return store

    def run_cli(self, argv: list[str]) -> tuple[int, dict | None, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch("backend.cli.knowledge.KnowledgeStore", self.open_store), \
                redirect_stdout(out), redirect_stderr(err):
            exit_code = dispatch(["knowledge", *argv])
        text = out.getvalue().strip()
        return exit_code, (json.loads(text) if text else None), err.getvalue()

    def add_prose(self, title: str, description: str = "why it was dropped") -> str:
        exit_code, result, _ = self.run_cli([
            "add", "--title", title, "--description", description, "--source-repo", "demo",
        ])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(result["added"])
        return result["id"]


class KnowledgeAddWithoutCodeTests(KnowledgeCliTestCase):
    def test_prose_entry_needs_no_code_and_defaults_to_convention(self) -> None:
        entry_id = self.add_prose("Lesson: seed-if-empty ships to nobody")

        exit_code, entry, _ = self.run_cli(["get", entry_id])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(entry["code"], "")
        self.assertEqual(entry["snippet_type"], "convention")

    def test_two_prose_entries_do_not_collide_as_duplicates(self) -> None:
        first = self.add_prose("Lesson one", "first lesson")
        second = self.add_prose("Lesson two", "second lesson")
        self.assertNotEqual(first, second)

        exit_code, result, _ = self.run_cli([
            "add", "--title", "Lesson two", "--description", "second lesson", "--source-repo", "demo",
        ])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertFalse(result["added"])

    def test_snippet_type_is_still_required_when_code_is_given(self) -> None:
        exit_code, result, err = self.run_cli([
            "add", "--title", "Snip", "--description", "d", "--code", "print(1)", "--source-repo", "demo",
        ])
        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertIsNone(result)
        self.assertIn("--snippet-type is required when --code", err)

    def test_code_hash_is_unchanged_for_code_entries(self) -> None:
        import hashlib
        self.assertEqual(compute_content_hash("print(1)", title="t"), hashlib.sha256(b"print(1)").hexdigest())
        self.assertNotEqual(compute_content_hash("", title="a", description="d"),
                            compute_content_hash("", title="b", description="d"))


class KnowledgeGetAndUpdateTests(KnowledgeCliTestCase):
    def test_get_unknown_id_exits_not_found(self) -> None:
        exit_code, result, err = self.run_cli(["get", "nope"])
        self.assertEqual(exit_code, EXIT_NOT_FOUND)
        self.assertIsNone(result)
        self.assertIn("not found", err)

    def test_update_keeps_id_bumps_revision_and_reindexes_search(self) -> None:
        entry_id = self.add_prose("Recipe", "the NAV recipe")

        exit_code, result, _ = self.run_cli([
            "update", entry_id, "--description", "the META recipe (quokka)", "--tag", "meta", "--tag", "recipe",
        ])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(result["id"], entry_id)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["updated"], ["description", "tags"])
        self.assertEqual(result["tags"], ["meta", "recipe"])
        self.assertEqual(result["title"], "Recipe")
        self.assertTrue(result["updated_at"])

        _, found, _ = self.run_cli(["search", "quokka"])
        self.assertEqual([e["id"] for e in found["entries"]], [entry_id])
        _, stale, _ = self.run_cli(["search", "NAV"])
        self.assertEqual(stale["entries"], [])

    def test_update_with_no_fields_is_a_usage_error(self) -> None:
        entry_id = self.add_prose("Recipe")
        exit_code, _, err = self.run_cli(["update", entry_id])
        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertIn("nothing to update", err)

    def test_update_unknown_id_exits_not_found(self) -> None:
        exit_code, _, err = self.run_cli(["update", "nope", "--title", "x"])
        self.assertEqual(exit_code, EXIT_NOT_FOUND)
        self.assertIn("not found", err)

    def test_update_refuses_to_collide_with_another_entry(self) -> None:
        self.add_prose("Lesson one", "same body")
        second = self.add_prose("Lesson two", "same body")

        exit_code, _, err = self.run_cli(["update", second, "--title", "Lesson one"])
        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertIn("already has this exact content", err)

        _, entry, _ = self.run_cli(["get", second])
        self.assertEqual(entry["title"], "Lesson two")
        self.assertEqual(entry["revision"], 1)

    def test_store_update_rejects_unknown_fields(self) -> None:
        store = self.open_store()
        entry = KnowledgeEntry.create(source_repo="r", source_file="f", snippet_type="pattern",
                                      title="t", description="d", code="c", tags=[])
        store.add_entry(entry)
        with self.assertRaises(ValueError):
            store.update_entry(entry.id, id="other")
        with self.assertRaises(KeyError):
            store.update_entry("missing", title="x")


class KnowledgeSearchRankingTests(KnowledgeCliTestCase):
    def seed(self) -> dict[str, str]:
        store = self.open_store()
        ids = {}
        for key, title, description, repo in (
            ("exact", "SQLite migration: add a column safely", "ALTER TABLE add column with a default, then backfill", "WaterFree"),
            ("column", "Column layout for the hand panel", "flex column wraps at narrow widths", "Paradoxia"),
            ("migration", "Data migration checklist", "how to migrate save files between versions", "dungeon"),
            ("other", "Godot camera follow", "third person camera arm", "VoxelGames"),
        ):
            entry = KnowledgeEntry.create(source_repo=repo, source_file="f", snippet_type="pattern",
                                          title=title, description=description, code=f"# {key}", tags=[])
            store.add_entry(entry)
            ids[key] = entry.id
        return ids

    def test_all_term_matches_rank_before_any_term_matches(self) -> None:
        ids = self.seed()
        exit_code, result, _ = self.run_cli(["search", "migration add column", "--limit", "3"])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(result["entries"][0]["id"], ids["exact"])
        self.assertEqual({e["id"] for e in result["entries"][1:]}, {ids["column"], ids["migration"]})

    def test_repo_filter_and_workspace_preference(self) -> None:
        ids = self.seed()
        _, only, _ = self.run_cli(["search", "migration", "--repo", "c:/projects/Dungeon"])
        self.assertEqual([e["id"] for e in only["entries"]], [ids["migration"]])

        _, preferred, _ = self.run_cli(["search", "migration column", "--workspace", "C:/Projects/Paradoxia"])
        self.assertEqual(preferred["preferred_repo"], "Paradoxia")
        # The exact (all-term) hit still wins its tier; within the any-term tier Paradoxia comes first.
        self.assertEqual(preferred["entries"][0]["id"], ids["exact"])
        self.assertEqual(preferred["entries"][1]["id"], ids["column"])

    def test_default_rows_omit_bodies_and_full_restores_them(self) -> None:
        ids = self.seed()
        _, compact, _ = self.run_cli(["search", "camera"])
        row = compact["entries"][0]
        self.assertEqual(row["id"], ids["other"])
        self.assertNotIn("code", row)
        self.assertEqual(row["code_chars"], len("# other"))
        self.assertIn("get <id>", compact["hint"])

        _, full, _ = self.run_cli(["search", "camera", "--full"])
        self.assertEqual(full["entries"][0]["code"], "# other")

        _, empty, _ = self.run_cli(["search", "zzzz"])
        self.assertEqual(empty["total"], 0)
        self.assertIn("0 entries match", empty["hint"])


class KnowledgeScopeTests(KnowledgeCliTestCase):
    def add(self, title: str, repo: str, **kw) -> str:
        argv = ["add", "--title", title, "--description", f"{title} lesson about retry backoff", "--source-repo", repo]
        for key, value in kw.items():
            argv += [f"--{key.replace('_', '-')}", value]
        exit_code, result, err = self.run_cli(argv)
        self.assertEqual(exit_code, EXIT_OK, err)
        return result["id"]

    def test_scope_defaults_and_search_visibility(self) -> None:
        shared = self.add("Retry with backoff", "WaterFree")
        mine = self.add("Retry backoff in the shop", "Paradoxia", scope="project")
        theirs = self.add("Retry backoff on the grid", "dungeon", scope="project")
        pack = self.add("Retry sound pack", "c:/projects/itch_assets", hierarchy_path="assets/packs/x")

        _, got, _ = self.run_cli(["get", pack])
        self.assertEqual(got["scope"], "assets")
        _, got, _ = self.run_cli(["get", shared])
        self.assertEqual(got["scope"], "global")

        _, default, _ = self.run_cli(["search", "retry backoff", "--workspace", "C:/Projects/Paradoxia"])
        self.assertEqual({e["id"] for e in default["entries"]}, {shared, mine})
        self.assertEqual(default["scope"], "default")

        # With no --workspace the cwd names the repo, so other projects' private entries stay hidden.
        _, here, _ = self.run_cli(["search", "retry backoff"])
        self.assertEqual({e["id"] for e in here["entries"]}, {shared})

        _, projects, _ = self.run_cli(["search", "retry backoff", "--scope", "project"])
        self.assertEqual({e["id"] for e in projects["entries"]}, {mine, theirs})

        _, everything, _ = self.run_cli(["search", "retry backoff", "--scope", "all"])
        self.assertEqual({e["id"] for e in everything["entries"]}, {shared, mine, theirs, pack})

        _, assets_only, _ = self.run_cli(["search", "retry", "--scope", "assets"])
        self.assertEqual([e["id"] for e in assets_only["entries"]], [pack])

        _, stats, _ = self.run_cli(["stats"])
        self.assertEqual(stats["by_scope"], {"global": 1, "project": 2, "assets": 1})

    def test_update_can_change_scope(self) -> None:
        entry_id = self.add("Retry with backoff", "WaterFree")
        exit_code, result, _ = self.run_cli(["update", entry_id, "--scope", "project"])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(result["scope"], "project")
        self.assertEqual(result["updated"], ["scope"])


class KnowledgeHookContextTests(KnowledgeCliTestCase):
    def run_hook(self, event: dict | str, *extra: str) -> tuple[int, dict | None, str]:
        raw = event if isinstance(event, str) else json.dumps(event)
        with patch("sys.stdin", io.StringIO(raw)):
            return self.run_cli(["hook-context", *extra])

    def test_injects_only_when_every_key_term_matches(self) -> None:
        entry_id = self.add_prose("Phaser container buttons drop taps when press-scale runs",
                                  "Guard setScale against a destroyed container before the pointer handler runs.")

        code, payload, err = self.run_hook({"user_prompt": "why do phaser container buttons drop taps?", "cwd": "C:/Projects/Paradoxia"})
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn(entry_id, payload["hookSpecificOutput"]["additionalContext"])
        self.assertIn("knowledge get", payload["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(set(payload["terms"]), {"phaser", "container", "buttons", "drop", "taps"})

        # unrelated long words break every tier of the all-terms gate: nothing is printed
        code, payload, _ = self.run_hook({"prompt": "phaser container buttons drop taps elephant giraffes"})
        self.assertEqual(code, EXIT_OK)
        self.assertIsNone(payload)

    def test_short_or_malformed_prompts_are_ignored_quietly(self) -> None:
        self.add_prose("Phaser container buttons drop taps", "guard setScale")
        for event in ({"prompt": "phaser"}, {}, "not json at all", ""):
            code, payload, err = self.run_hook(event)
            self.assertEqual(code, EXIT_OK)
            self.assertIsNone(payload)
            self.assertEqual(err, "")

    def test_long_prompt_falls_back_to_its_specific_identifiers(self) -> None:
        entry_id = self.add_prose("NearGrassField LOD_BANDS density", "the near band dominates the view")
        prompt = ("Could you please have another careful look at why the grass around the player still feels "
                  "far too thick even after we changed NearGrassField and LOD_BANDS yesterday evening")
        code, payload, _ = self.run_hook({"prompt": prompt})
        self.assertEqual(code, EXIT_OK)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["entries"][0]["id"], entry_id)

    def test_hook_firings_are_counted_by_the_usage_log(self) -> None:
        log = self.db_path.parent / "usage.jsonl"
        with patch.dict(os.environ, {"WATERFREE_USAGE_LOG": str(log)}, clear=False):
            entry_id = self.add_prose("Phaser container buttons drop taps", "guard setScale")
            self.run_hook({"prompt": "phaser container buttons drop taps"})
        from backend.cli import usage_log
        hook_records = [r for r in usage_log.read_records([log]) if r["action"] == "hook-context"]
        self.assertEqual(len(hook_records), 1)
        self.assertEqual(hook_records[0]["hits"], 1)
        self.assertEqual(hook_records[0]["hit_ids"], [entry_id])


class KnowledgeAddQualityGateTests(KnowledgeCliTestCase):
    def test_near_duplicate_title_is_reported_but_the_add_still_happens(self) -> None:
        first = self.add_prose("Phaser container buttons drop taps when press-scale runs",
                               "Guard setScale against a destroyed container.")
        exit_code, result, err = self.run_cli([
            "add", "--title", "Phaser container buttons drop taps on press scale",
            "--description", "Same lesson, filed again.", "--source-repo", "demo",
        ])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(result["added"])
        warnings = result["warnings"]
        self.assertTrue(any(w["kind"] == "near_duplicate" and first in w["ids"] for w in warnings), warnings)
        self.assertTrue(any(w["kind"] == "no_hierarchy" for w in warnings))
        self.assertIn("near-duplicate", err)
        self.assertIn("knowledge update", err)

    def test_trace_like_description_is_flagged_and_insight_is_not(self) -> None:
        exit_code, result, _ = self.run_cli([
            "add", "--title", "Fixing the camera arm", "--source-repo", "demo",
            "--hierarchy-path", "godot/camera",
            "--description", "I ran the scene and then I tried setting the arm to 6.5. Then I checked the raycast and it worked.",
        ])
        self.assertEqual(exit_code, EXIT_OK)
        kinds = {w["kind"] for w in result["warnings"]}
        self.assertIn("reads_like_a_trace", kinds)
        self.assertNotIn("no_hierarchy", kinds)

        exit_code, result, err = self.run_cli([
            "add", "--title", "Camera arm must be shorter than canopy spacing", "--source-repo", "demo",
            "--hierarchy-path", "godot/camera",
            "--description", "Keep the third-person camera arm below the mean canopy spacing (1/sqrt(density)); otherwise the camera lives inside trunks.",
        ])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(err, "")

    def test_no_check_skips_the_gate(self) -> None:
        self.add_prose("Phaser container buttons drop taps")
        exit_code, result, err = self.run_cli([
            "add", "--title", "Phaser container buttons drop taps again", "--description", "dup",
            "--source-repo", "demo", "--no-check",
        ])
        self.assertEqual(exit_code, EXIT_OK)
        self.assertNotIn("warnings", result)
        self.assertEqual(err, "")


class KnowledgeReminderHookTests(KnowledgeCliTestCase):
    def run_event(self, action: str, event: dict | str) -> tuple[int, dict | None, str]:
        raw = event if isinstance(event, str) else json.dumps(event)
        with patch("sys.stdin", io.StringIO(raw)):
            return self.run_cli([action])

    def test_session_hook_is_silent_on_an_empty_store_and_orients_otherwise(self) -> None:
        code, payload, err = self.run_event("hook-session", {"cwd": "C:/Projects/Paradoxia"})
        self.assertEqual((code, payload, err), (EXIT_OK, None, ""))

        shared = self.add_prose("Retry with backoff", "shared lesson")
        exit_code, mine, _ = self.run_cli(["add", "--title", "Shop card hit zone", "--description", "own lesson",
                                           "--source-repo", "Paradoxia", "--scope", "project"])
        self.assertEqual(exit_code, EXIT_OK)
        code, payload, _ = self.run_event("hook-session", {"cwd": "C:/Projects/Paradoxia", "source": "startup"})
        self.assertEqual(code, EXIT_OK)
        text = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("2 shared lessons available (1 filed from Paradoxia)", text)
        self.assertIn(mine["id"], text)
        self.assertNotIn(shared, text)
        self.assertLess(len(text), 600)

        code, payload, _ = self.run_event("hook-session", "garbage")
        self.assertEqual((code, payload), (EXIT_OK, None))

    def test_stop_hook_asks_once_for_a_lesson_and_stays_quiet_otherwise(self) -> None:
        lesson = ("Fixed. It turned out that the camera raycast was hitting the player's own collider, "
                  "which is why the arm collapsed; after three attempts the collision mask exclusion worked.")
        code, payload, _ = self.run_event("hook-stop", {"last_assistant_message": lesson, "cwd": "C:/Projects/VoxelGames"})
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("waterfree knowledge add", payload["reason"])
        self.assertIn("--source-repo VoxelGames", payload["reason"])
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertEqual(payload["markers"], ["several attempts", "turned out"])

        # already asked this turn
        code, payload, _ = self.run_event("hook-stop", {"last_assistant_message": lesson, "stop_hook_active": True})
        self.assertEqual((code, payload), (EXIT_OK, None))
        # already filed
        code, payload, _ = self.run_event("hook-stop", {"last_assistant_message": lesson + " I filed it with waterfree knowledge add."})
        self.assertEqual((code, payload), (EXIT_OK, None))
        # ordinary message
        plain = "Renamed the helper, updated the three call sites, and the suite passes. Nothing else changed in the module."
        code, payload, _ = self.run_event("hook-stop", {"last_assistant_message": plain})
        self.assertEqual((code, payload), (EXIT_OK, None))
        # short message
        code, payload, _ = self.run_event("hook-stop", {"last_assistant_message": "root cause found"})
        self.assertEqual((code, payload), (EXIT_OK, None))


class KnowledgeLegacyDatabaseTests(unittest.TestCase):
    """Databases created before `update` existed gain the columns and trigger on open."""

    def test_legacy_rows_can_be_read_updated_and_searched(self) -> None:
        db_path = make_test_dir(self, prefix="knowledge-legacy-") / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE knowledge_entries (
                id TEXT PRIMARY KEY, source_repo TEXT NOT NULL, source_file TEXT NOT NULL,
                snippet_type TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                code TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
                content_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                source_repo_url TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO knowledge_entries VALUES
                ('old1', 'r', 'f', 'pattern', 'Old title', 'old words', 'old code', '[]', 'h1', '2026-01-01', ''),
                ('old2', 'c:/projects/itch_assets', 'f', 'pattern', 'Asset pack: kenney', 'cc0', 'k', '[]', 'h2', '2026-01-01', ''),
                ('old3', 'Paradoxia', 'f', 'pattern', 'Paradoxia: shop card hit zone', 'glow', 'p', '[]', 'h3', '2026-01-01', ''),
                ('old4', 'Paradoxia', 'f', 'pattern', 'Phaser containers drop taps', 'taps', 'q', '[]', 'h4', '2026-01-01', '');
            CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                title, description, tags, code, content='knowledge_entries', content_rowid='rowid');
            INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild');
        """)
        conn.commit()
        conn.close()

        store = KnowledgeStore(str(db_path))
        self.addCleanup(store.close)
        old = store.get_entry("old1")
        self.assertIsNotNone(old)
        self.assertEqual(old.revision, 1)
        self.assertEqual(old.updated_at, "")
        self.assertEqual(old.scope, "global")

        revised = store.update_entry("old1", description="new words quokka")
        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.scope, "global")
        self.assertEqual([e.id for e in store.search("quokka")], ["old1"])
        self.assertEqual(store.search("words old"), store.search("quokka"))
        # backfill: old3's title names its own repo -> project; the others stay global
        self.assertEqual(store.scope_counts(), {"global": 3, "project": 1})
        self.assertEqual(store.get_entry("old3").scope, "project")


if __name__ == "__main__":
    unittest.main()
