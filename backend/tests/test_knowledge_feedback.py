"""Knowledge-store fixes from the 2026-11 agent feedback round (KB-* backlog keys).

Each class names the backlog key whose acceptance criteria it covers.
"""

import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.cli._common import EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE
from backend.cli.dispatcher import dispatch
from backend.cli.knowledge import _trace_evidence
from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.source_ref import ReaderContext, capture_source_ref, find_worktree_root, repo_name
from backend.knowledge.store import AmbiguousEntryError, KnowledgeStore
from backend.test_support import make_temp_dir as make_test_dir
from backend.tests.test_knowledge_cli import KnowledgeCliTestCase


def _entry(title: str, *, repo: str = "demo", description: str = "", code: str = "",
           path: str = "", entry_id: str | None = None, created_at: str | None = None) -> KnowledgeEntry:
    entry = KnowledgeEntry.create(source_repo=repo, source_file="", snippet_type="convention", title=title,
                                  description=description or f"{title} lesson", code=code, tags=[],
                                  hierarchy_path=path or None)
    if entry_id:
        entry.id = entry_id
    if created_at:
        entry.created_at = created_at
    return entry


class TraceGateTests(KnowledgeCliTestCase):
    """KB-ADD-002: narratives warn, numbered instructions do not, and warnings quote their cause."""

    HOW_TO = (
        "1. Set ARM_LENGTH to 6.5 m in camera_rig.gd (measured 4.2 ms per frame at 2,000 trees).\n"
        "2. Run the coplanar check before merging; KB-ADD-002 tracks the regression.\n"
        "3. Keep the raycast mask excluding layer 3, because the player collider is on it.\n"
        "Step 4: re-measure after changing density."
    )

    def test_imperative_numbered_list_with_numbers_and_task_key_does_not_warn(self) -> None:
        self.assertEqual(_trace_evidence(self.HOW_TO), [])
        code, result, err = self.run_cli([
            "add", "--title", "Camera rig tuning recipe", "--description", self.HOW_TO,
            "--source-repo", "demo", "--hierarchy-path", "godot/camera",
        ])
        self.assertEqual(code, EXIT_OK, err)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(err, "")

    def test_first_person_narrative_still_warns_and_quotes_the_sentence(self) -> None:
        text = "I ran the exporter, then I tried the legacy flag. It worked."
        self.assertEqual(_trace_evidence(text), ["I ran the exporter, then I tried the legacy flag."])
        code, result, err = self.run_cli([
            "add", "--title", "Exporter legacy flag", "--description", text,
            "--source-repo", "demo", "--hierarchy-path", "tools/export",
        ])
        self.assertEqual(code, EXIT_OK)
        trace = next(w for w in result["warnings"] if w["kind"] == "reads_like_a_trace")
        self.assertEqual(trace["evidence"], ["I ran the exporter, then I tried the legacy flag."])
        # Piped (not a TTY): advisory output lives only in the JSON (KB-STDERR-001).
        self.assertEqual(err, "")
        self.assertIn(f"saved anyway as {result['id']}; revise with `knowledge update {result['id']}`",
                      result["notes"])

    def test_one_narrating_clause_is_not_a_log_and_numbered_narration_is(self) -> None:
        self.assertEqual(_trace_evidence("If the build fails, then I check the log first."), [])
        self.assertTrue(_trace_evidence("1. I opened the scene.\n2. We noticed the arm clipped."))

    def test_near_duplicate_warning_names_the_shared_title_words(self) -> None:
        self.add_prose("Phaser container buttons drop taps on press scale")
        code, result, err = self.run_cli([
            "add", "--title", "Phaser container buttons drop taps", "--description", "x",
            "--source-repo", "other",
        ])
        self.assertEqual(code, EXIT_USAGE)
        near = next(w for w in result["warnings"] if w["kind"] == "near_duplicate")
        self.assertEqual(set(near["shared_terms"]), {"phaser", "container", "buttons", "drop", "taps"})
        self.assertEqual(err, "")


class AddOutcomeTests(KnowledgeCliTestCase):
    """KB-ADD-001: an agent can always tell whether `add` stored the entry."""

    TRACE = "I ran the scene and then I tried an arm of 6.5. Then I checked the raycast."

    def test_trace_warned_add_says_saved_anyway_and_leads_with_the_outcome(self) -> None:
        code, result, err = self.run_cli([
            "add", "--title", "Camera arm below canopy spacing", "--description", self.TRACE,
            "--source-repo", "demo", "--hierarchy-path", "godot/camera",
        ])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(list(result)[:3], ["added", "id", "saved"])
        self.assertTrue(result["added"])
        self.assertTrue(result["saved"])
        entry_id = result["id"]
        self.assertIsNotNone(self.open_store().get_entry(entry_id))
        # Piped: nothing on stderr (KB-STDERR-001); the JSON carries the outcome and the advice.
        self.assertEqual(err, "")
        self.assertIn(f"saved anyway as {entry_id}; revise with `knowledge update {entry_id}`", result["notes"])
        self.assertTrue(result["summary"].startswith(f"saved: {entry_id}"), result["summary"])
        self.assertEqual(list(result)[-1], "warnings")

    def test_immediate_reworded_retry_is_blocked_naming_the_just_saved_id(self) -> None:
        _, first, _ = self.run_cli([
            "add", "--title", "Camera arm below canopy spacing", "--description", self.TRACE,
            "--source-repo", "demo", "--hierarchy-path", "godot/camera",
        ])
        code, retry, err = self.run_cli([
            "add", "--title", "Camera arm below canopy spacing",
            "--description", "Keep the camera arm shorter than the mean canopy spacing.",
            "--source-repo", "demo", "--hierarchy-path", "godot/camera",
        ])
        saved = first["id"]
        self.assertEqual(code, EXIT_USAGE)
        self.assertFalse(retry["saved"])
        self.assertTrue(retry["blocked"])
        self.assertEqual(retry["recent_id"], saved)
        self.assertIn(f"you saved this as {saved} moments ago", retry["message"])
        self.assertEqual(err, "")
        self.assertIn(f"you saved this as {saved} moments ago", retry["summary"])
        self.assertIn(f"`knowledge update {saved}`", retry["summary"])
        self.assertEqual(self.open_store().total_entries(), 1)

    def test_an_old_twin_or_another_repos_twin_gets_the_generic_block(self) -> None:
        store = self.open_store()
        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        store.add_entry(_entry("Camera arm below canopy spacing", created_at=hour_ago))
        store.add_entry(_entry("Camera arm below canopy spacing again", repo="elsewhere",
                               description="other repo"))
        code, result, err = self.run_cli([
            "add", "--title", "Camera arm below canopy spacing", "--description", "new words",
            "--source-repo", "demo",
        ])
        self.assertEqual(code, EXIT_USAGE)
        self.assertNotIn("recent_id", result)
        self.assertIn("near-duplicate of", result["summary"])

    def test_force_on_an_exact_duplicate_reports_the_existing_id_and_revision(self) -> None:
        entry_id = self.add_prose("Seed-if-empty ships to nobody", "Track offered resources in a marker file.")
        # tags are not part of the content hash: the content stays identical at revision 2
        self.run_cli(["update", entry_id, "--tag", "seeding"])
        code, result, err = self.run_cli([
            "add", "--title", "Seed-if-empty ships to nobody", "--description",
            "Track offered resources in a marker file.", "--source-repo", "demo", "--force",
        ])
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(result["added"])
        self.assertEqual(result["id"], entry_id)
        self.assertEqual(result["duplicate_of"], entry_id)
        self.assertEqual(result["revision"], 2)
        self.assertIn(f"identical content is already entry {entry_id} (revision 2)", result["summary"])

    def test_clean_add_keeps_stderr_empty(self) -> None:
        code, result, err = self.run_cli([
            "add", "--title", "Clean lesson about ports", "--description", "Bind to 127.0.0.1 in tests.",
            "--source-repo", "demo", "--hierarchy-path", "testing/network",
        ])
        self.assertEqual((code, err), (EXIT_OK, ""))
        self.assertEqual(result["summary"], f"saved: {result['id']}")


class OwnRepoSearchTests(KnowledgeCliTestCase):
    """KB-SEARCH-001: own-repo all-term matches are never cut in favour of other repos."""

    def seed(self, store: KnowledgeStore, others: int = 12) -> list[str]:
        for i in range(others):
            store.add_entry(_entry(f"Tween easing tween easing note {i}", repo=f"other{i % 3}",
                                   description="tween easing tween easing tween easing"))
        own = []
        padding = " ".join(f"filler{n}" for n in range(120))
        for i in range(2):
            entry = _entry(f"Own lesson {i}", repo="BrickWild",
                           description=f"tween and easing, buried in a long description. {padding}")
            store.add_entry(entry)
            own.append(entry.id)
        return own

    def test_own_entries_with_the_lowest_bm25_still_come_first(self) -> None:
        own = self.seed(self.open_store())
        _, neutral, _ = self.run_cli(["search", "tween easing", "--limit", "10", "--workspace", "C:/Projects/Elsewhere"])
        self.assertTrue(set(own).isdisjoint(e["id"] for e in neutral["entries"]), "fixture: own rows rank last")

        code, result, _ = self.run_cli(["search", "tween easing", "--limit", "10",
                                        "--workspace", "C:/Projects/BrickWild"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(result["entries"]), 10)
        self.assertEqual({e["id"] for e in result["entries"][:2]}, set(own))

    def test_like_fallback_also_prefers_before_cutting(self) -> None:
        store = self.open_store()
        own = self.seed(store, others=10)
        with patch.object(KnowledgeStore, "_fts_tier", side_effect=sqlite3.OperationalError("no fts")):
            found = store.search("tween and easing", limit=2, prefer_repo="BrickWild")
        self.assertEqual({e.id for e in found}, set(own))

    def test_repo_filter_is_applied_before_the_sql_limit(self) -> None:
        store = self.open_store()
        own = self.seed(store, others=12)
        found = store.search("tween easing", limit=1, repo="c:/projects/brickwild")
        self.assertEqual(len(found), 1)
        self.assertIn(found[0].id, own)


class IdPrefixTests(KnowledgeCliTestCase):
    """KB-CLI-002: get/update/delete accept a unique id prefix."""

    def seed(self) -> None:
        store = self.open_store()
        store.add_entry(_entry("Alpha", entry_id="abcdef01-0000-4000-8000-000000000001"))
        store.add_entry(_entry("Beta", entry_id="abcdef02-0000-4000-8000-000000000002"))
        store.add_entry(_entry("Gamma", entry_id="d35c8db3-0000-4000-8000-000000000003"))

    def test_unique_prefix_resolves_for_get_update_and_delete(self) -> None:
        self.seed()
        code, got, _ = self.run_cli(["get", "d35c8db3"])
        self.assertEqual((code, got["title"]), (EXIT_OK, "Gamma"))
        code, _, _ = self.run_cli(["get", "D35C8D"])
        self.assertEqual(code, EXIT_OK)
        code, revised, _ = self.run_cli(["update", "abcdef01", "--title", "Alpha two"])
        self.assertEqual((code, revised["id"]), (EXIT_OK, "abcdef01-0000-4000-8000-000000000001"))
        code, deleted, _ = self.run_cli(["delete", "d35c8d"])
        self.assertEqual((code, deleted["id"]), (EXIT_OK, "d35c8db3-0000-4000-8000-000000000003"))
        self.assertIsNone(self.open_store().get_entry("d35c8db3-0000-4000-8000-000000000003"))

    def test_ambiguous_prefix_lists_the_candidates(self) -> None:
        self.seed()
        for argv in (["get", "abcdef"], ["update", "abcdef0", "--title", "x"], ["delete", "abcdef"]):
            code, result, err = self.run_cli(argv)
            self.assertEqual(code, EXIT_USAGE, argv)
            self.assertIsNone(result)
            self.assertIn("matches 2 entries", err)
            self.assertIn("abcdef01-0000-4000-8000-000000000001  Alpha", err)
            self.assertIn("abcdef02-0000-4000-8000-000000000002  Beta", err)
        self.assertEqual(self.open_store().total_entries(), 3)
        with self.assertRaises(AmbiguousEntryError) as caught:
            self.open_store().get_entry("abcdef")
        self.assertEqual(len(caught.exception.candidates), 2)

    def test_too_short_prefix_is_refused(self) -> None:
        self.seed()
        code, _, err = self.run_cli(["get", "d35c8"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("too short", err)

    def test_prefix_lookups_still_count_as_retrievals(self) -> None:
        from backend.knowledge.consolidate import retrieval_counts
        ids = ["abcdef01-0000-4000-8000-000000000001", "abcdef02-0000-4000-8000-000000000002",
               "d35c8db3-0000-4000-8000-000000000003"]
        records = [{"area": "knowledge", "action": "get", "argv": [arg]}
                   for arg in ("d35c8db3", "abcdef", "abcdef01", ids[1])]
        counts, considered = retrieval_counts(records, since=None, known_ids=ids)
        self.assertEqual(considered, 4)
        self.assertEqual(counts, {ids[2]: 1, ids[0]: 1, ids[1]: 1})  # the ambiguous 'abcdef' credits nobody

    def test_non_hex_misses_are_still_not_found_and_delete_ignores_titles(self) -> None:
        self.seed()
        self.assertEqual(self.run_cli(["get", "nope"])[0], EXIT_NOT_FOUND)
        code, result, _ = self.run_cli(["delete", "Gamma"])
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertFalse(result["deleted"])
        self.assertEqual(self.open_store().total_entries(), 3)


class QuietAndAppendTests(KnowledgeCliTestCase):
    """KB-CLI-001: --quiet receipts, append flags, --context-file, large-replace warning."""

    def test_quiet_add_and_update_print_a_small_receipt(self) -> None:
        out = io.StringIO()
        with patch("backend.cli.knowledge.KnowledgeStore", self.open_store), redirect_stdout(out), \
                redirect_stderr(io.StringIO()):
            dispatch(["knowledge", "add", "--title", "Quiet lesson about caching", "--description", "d",
                      "--source-repo", "demo", "--hierarchy-path", "perf/cache", "--quiet"])
        raw = out.getvalue()
        self.assertLess(len(raw.encode("utf-8")), 200)
        receipt = json.loads(raw)
        self.assertEqual(set(receipt), {"id", "added", "revision"})
        self.assertEqual((receipt["added"], receipt["revision"]), (True, 1))

        out = io.StringIO()
        with patch("backend.cli.knowledge.KnowledgeStore", self.open_store), redirect_stdout(out):
            dispatch(["knowledge", "update", receipt["id"], "--context", "only on Windows", "--tag", "cache",
                      "--quiet"])
        raw = out.getvalue()
        self.assertLess(len(raw.encode("utf-8")), 200)
        self.assertEqual(json.loads(raw), {"id": receipt["id"], "updated": ["context", "tags"], "revision": 2})

    def test_append_flags_keep_the_old_text(self) -> None:
        entry_id = self.add_prose("Append target", "Original description.")
        self.run_cli(["update", entry_id, "--context", "First caveat."])
        code, result, err = self.run_cli(["update", entry_id, "--append-context", "Second caveat.",
                                          "--append-description", "More detail."])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(result["context"], "First caveat.\n\nSecond caveat.")
        self.assertEqual(result["description"], "Original description.\n\nMore detail.")
        self.assertEqual(result["updated"], ["context", "description"])
        self.assertNotIn("warning", err)

    def test_context_file_on_add_and_update_including_stdin(self) -> None:
        path = self.db_path.parent / "context.md"
        path.write_text("Caveat line one.\nCaveat line two.", encoding="utf-8")
        code, added, _ = self.run_cli(["add", "--title", "Context from file", "--description", "d",
                                       "--source-repo", "demo", "--context-file", str(path)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.open_store().get_entry(added["id"]).context, "Caveat line one.\nCaveat line two.")
        with patch("sys.stdin", io.StringIO("From stdin.")):
            code, revised, _ = self.run_cli(["update", added["id"], "--context-file", "-"])
        self.assertEqual((code, revised["context"]), (EXIT_OK, "From stdin."))
        with patch("sys.stdin", io.StringIO("x")):
            code, _, err = self.run_cli(["update", added["id"], "--context-file", "-", "--code-file", "-"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("cannot both read stdin", err)

    def test_a_replace_that_discards_most_of_the_old_text_warns(self) -> None:
        entry_id = self.add_prose("Replace target", "d")
        context = "Applies only to third-person rigs with a spring arm and a collision mask on layer three."
        self.run_cli(["update", entry_id, "--context", context])
        code, result, err = self.run_cli(["update", entry_id, "--context", "Applies to rigs."])
        self.assertEqual((code, err), (EXIT_OK, ""))
        (warning,) = result["warnings"]
        self.assertIn("discards", warning)
        self.assertIn("of the old context", warning)
        self.assertIn("--append-context", warning)
        self.assertIn("--restore-revision 2", warning)
        # extending the text is not a discard
        _, result, _ = self.run_cli(["update", entry_id, "--context", "Applies to rigs. And to cranes too."])
        self.assertNotIn("warnings", result)


class BriefSearchTests(KnowledgeCliTestCase):
    """KB-SEARCH-002: titles-only rows."""

    def seed(self) -> None:
        store = self.open_store()
        for i in range(10):
            store.add_entry(KnowledgeEntry.create(
                source_repo="BrickWild", source_file="src/village/lot_planner.gd", snippet_type="pattern",
                title=f"Lot planner splits plots along street frontage {i}",
                description="Long description " * 40, code=f"func plan_{i}():\n    pass\n" * 30,
                tags=["godot", "village"], context="caveat " * 50, hierarchy_path="procedural/villages/lots",
            ))

    def test_brief_rows_carry_only_four_fields_and_stay_small(self) -> None:
        self.seed()
        log = self.db_path.parent / "usage.jsonl"
        out = io.StringIO()
        with patch.dict(os.environ, {"WATERFREE_USAGE_LOG": str(log)}, clear=False), \
                patch("backend.cli.knowledge.KnowledgeStore", self.open_store), redirect_stdout(out):
            code = dispatch(["knowledge", "search", "lot planner frontage", "--brief"])
        self.assertEqual(code, EXIT_OK)
        raw = out.getvalue()
        result = json.loads(raw)
        self.assertEqual(result["mode"], "brief")
        self.assertEqual(len(result["entries"]), 10)
        for row in result["entries"]:
            self.assertEqual(set(row), {"id", "title", "hierarchy_path", "source_repo"})
        # 10 rows with 50-char titles, a 24-char path and 36-char ids: about 1.8 KB
        # whole, against ~6 KB for the same rows in the default shape.
        rows_bytes = sum(len(json.dumps(row, separators=(",", ":"))) for row in result["entries"])
        self.assertLess(rows_bytes, 1800)
        self.assertLess(len(raw), 2100)
        _, default, _ = self.run_cli(["search", "lot planner frontage"])
        self.assertLess(len(raw) * 3, len(json.dumps(default, indent=2)))

        from backend.cli import usage_log
        record = usage_log.read_records([log])[-1]
        self.assertIn("--brief", record["argv"])
        self.assertEqual(record["result_bytes"], len(raw))
        self.assertEqual(record["hits"], 10)

    def test_brief_and_full_are_exclusive(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.run_cli(["search", "x", "--brief", "--full"])


class BrowseTests(KnowledgeCliTestCase):
    """KB-BROWSE-001: positional path, near-miss hint, compact rows."""

    def seed(self) -> str:
        store = self.open_store()
        entry = KnowledgeEntry.create(source_repo="demo", source_file="", snippet_type="pattern",
                                      title="Gable roofs", description="Pitch them at 40 degrees.",
                                      code="func roof(): pass", tags=[], context="not for flat roofs",
                                      hierarchy_path="procedural/buildings")
        store.add_entry(entry)
        store.add_entry(_entry("Dungeon keys", path="procedural/dungeons"))
        return entry.id

    def test_positional_path_works(self) -> None:
        entry_id = self.seed()
        code, positional, _ = self.run_cli(["browse", "Procedural \\ Buildings", "--include-entries"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(positional["path"], "procedural/buildings")
        self.assertEqual([e["id"] for e in positional["entries"]], [entry_id])
        _, flagged, _ = self.run_cli(["browse", "--path", "procedural/buildings", "--include-entries"])
        self.assertEqual(positional, flagged)
        code, _, err = self.run_cli(["browse", "procedural", "--path", "other"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("two different paths", err)

    def test_a_missing_path_returns_a_hint_naming_the_nearest_paths(self) -> None:
        self.seed()
        code, result, _ = self.run_cli(["browse", "Procedural buildings"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(result["entry_count"], 0)
        self.assertIn("procedural/buildings", result["hint"])
        self.assertEqual(result["suggestions"][0]["path"], "procedural/buildings")
        _, typo, _ = self.run_cli(["browse", "procedural/biuldings"])
        self.assertEqual(typo["suggestions"][0]["path"], "procedural/buildings")
        _, tail, _ = self.run_cli(["browse", "dungeons"])
        self.assertEqual(tail["suggestions"][0]["path"], "procedural/dungeons")
        _, nothing, _ = self.run_cli(["browse", "zzzzzz"])
        self.assertEqual(nothing["suggestions"], [])
        self.assertIn("browse --depth 1", nothing["hint"])
        _, search, _ = self.run_cli(["search", "roofs", "--path", "procedural buildings"])
        self.assertIn("procedural/buildings", search["hint"])

    def test_separator_twins_are_listed_when_both_exist(self) -> None:
        self.seed()
        self.open_store().add_entry(_entry("House slopes", path="procedural buildings/house"))
        _, result, _ = self.run_cli(["browse", "procedural buildings"])
        self.assertEqual(result["entry_count"], 1)
        self.assertEqual([s["path"] for s in result["see_also"]], ["procedural/buildings"])

    def test_include_entries_rows_are_compact_unless_full(self) -> None:
        self.seed()
        _, compact, _ = self.run_cli(["browse", "procedural/buildings", "--include-entries"])
        row = compact["entries"][0]
        self.assertNotIn("code", row)
        self.assertNotIn("context", row)
        self.assertEqual(row["code_chars"], len("func roof(): pass"))
        _, search, _ = self.run_cli(["search", "gable roofs"])
        self.assertEqual(set(row), set(search["entries"][0]))
        _, full, _ = self.run_cli(["browse", "procedural/buildings", "--include-entries", "--full"])
        self.assertEqual(full["entries"][0]["code"], "func roof(): pass")
        self.assertEqual(full["entries"][0]["context"], "not for flat roofs")
        _, listed, _ = self.run_cli(["browse", "procedural/buildings", "--list"])
        self.assertEqual(listed["entries"], compact["entries"])


class HistoryTests(KnowledgeCliTestCase):
    """KB-HISTORY-001: replaced values are kept and can be restored."""

    def test_replace_then_recover(self) -> None:
        entry_id = self.add_prose("History target", "the description")
        self.run_cli(["update", entry_id, "--context", "A carefully written 188-char caveat about spring arms."])
        self.run_cli(["update", entry_id, "--context", "oops", "--title", "History target renamed"])

        code, history, _ = self.run_cli(["history", entry_id[:8]])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(history["revision"], 3)
        rows = history["history"]
        self.assertEqual([(r["revision"], r["field"]) for r in rows],
                         [(2, "title"), (2, "context"), (1, "context")])
        self.assertEqual(rows[1]["old_value"], "A carefully written 188-char caveat about spring arms.")
        _, only_context, _ = self.run_cli(["history", entry_id, "--field", "context"])
        self.assertEqual({r["field"] for r in only_context["history"]}, {"context"})

        code, restored, _ = self.run_cli(["update", entry_id, "--restore-revision", "2", "--restore-field", "context"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(restored["context"], "A carefully written 188-char caveat about spring arms.")
        self.assertEqual(restored["title"], "History target renamed")
        self.assertEqual(restored["updated"], ["context"])
        self.assertEqual(restored["revision"], 4)
        # the restore is itself recorded, so it can be undone too
        _, after, _ = self.run_cli(["history", entry_id])
        self.assertEqual((after["history"][0]["change"], after["history"][0]["old_value"]), ("restore", "oops"))

        code, full_restore, _ = self.run_cli(["update", entry_id, "--restore-revision", "1"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((full_restore["title"], full_restore["context"]), ("History target", ""))

    def test_restore_errors_are_usage_errors(self) -> None:
        entry_id = self.add_prose("Restore errors", "d")
        code, _, err = self.run_cli(["update", entry_id, "--restore-revision", "1"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("between 1 and 0", err)
        self.run_cli(["update", entry_id, "--tag", "x"])
        code, _, err = self.run_cli(["update", entry_id, "--restore-revision", "1", "--title", "y"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("cannot be combined with --title", err)
        code, _, err = self.run_cli(["update", entry_id, "--restore-field", "title"])
        self.assertEqual(code, EXIT_USAGE)

    def test_a_deleted_entry_stays_readable_in_history(self) -> None:
        entry_id = self.add_prose("Deleted lesson", "worth keeping after all")
        self.run_cli(["delete", entry_id])
        code, history, _ = self.run_cli(["history", entry_id[:8]])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(history["deleted"])
        values = {r["field"]: r["old_value"] for r in history["history"] if r["change"] == "delete"}
        self.assertEqual(values["description"], "worth keeping after all")
        self.assertEqual(values["title"], "Deleted lesson")
        self.assertEqual(self.run_cli(["history", "ffffffff"])[0], EXIT_NOT_FOUND)


class SchemaMigrationTests(unittest.TestCase):
    """KB-HISTORY-001 / KB-BRANCH-001: additive migrations, idempotent on re-open."""

    OLD_SCHEMA = """
        CREATE TABLE knowledge_repos (name TEXT PRIMARY KEY, local_path TEXT NOT NULL,
            remote_url TEXT NOT NULL DEFAULT '', entry_count INTEGER NOT NULL DEFAULT 0, last_indexed TEXT NOT NULL);
        CREATE TABLE knowledge_entries (
            id TEXT PRIMARY KEY, source_repo TEXT NOT NULL, source_file TEXT NOT NULL,
            snippet_type TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
            code TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', content_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL, source_repo_url TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '', hierarchy_path TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
            scope TEXT NOT NULL DEFAULT 'global');
        INSERT INTO knowledge_entries (id, source_repo, source_file, snippet_type, title, description, code,
            tags, content_hash, created_at, context, revision)
        VALUES ('old1', 'r', 'f', 'pattern', 'Old title', 'old words', 'old code', '["t"]', 'h1',
                '2026-01-01', 'old context', 3);
        CREATE VIRTUAL TABLE knowledge_fts USING fts5(
            title, description, tags, code, content='knowledge_entries', content_rowid='rowid');
        INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild');
    """

    def make_db(self) -> Path:
        db_path = make_test_dir(self, prefix="knowledge-migrate-") / "old.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(self.OLD_SCHEMA)
        conn.commit()
        conn.close()
        return db_path

    def test_pre_history_database_opens_without_data_loss_and_records_history(self) -> None:
        db_path = self.make_db()
        store = KnowledgeStore(str(db_path))
        self.addCleanup(store.close)
        old = store.get_entry("old1")
        self.assertEqual((old.context, old.revision, old.source_ref, old.tags), ("old context", 3, None, ["t"]))
        revised = store.update_entry("old1", context="new context")
        self.assertEqual(revised.revision, 4)
        _, _, rows = store.history("old1")
        self.assertEqual([(r["revision"], r["field"], r["old_value"]) for r in rows], [(3, "context", "old context")])
        restored, fields = store.restore_revision("old1", 3)
        self.assertEqual((restored.context, fields), ("old context", ["context"]))

    def test_reopening_an_already_migrated_database_is_a_no_op(self) -> None:
        db_path = self.make_db()
        first = KnowledgeStore(str(db_path))
        first.update_entry("old1", description="changed once")
        first.close()
        # a second and third open (e.g. the real DB, migrated by an earlier build) must not fail or duplicate
        for _ in range(2):
            store = KnowledgeStore(str(db_path))
            self.assertEqual(store.get_entry("old1").description, "changed once")
            self.assertEqual(len(store.history("old1")[2]), 1)
            store.close()
        conn = sqlite3.connect(db_path)
        self.addCleanup(conn.close)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(knowledge_entries)")]
        self.assertEqual(cols.count("source_ref"), 1)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name = 'knowledge_entry_history'")]
        self.assertEqual(tables, ["knowledge_entry_history"])


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=kb@test", "-c", "user.name=kb", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class SourceRefTests(KnowledgeCliTestCase):
    """KB-BRANCH-001: record branch / worktree / committed status of source_file."""

    def make_repo(self) -> tuple[Path, Path]:
        base = make_test_dir(self, prefix="knowledge-git-")
        main = base / "BrickWild"
        main.mkdir()
        _git(main, "init", "-q", "-b", "main")
        (main / "tracked.gd").write_text("extends Node\n", encoding="utf-8")
        _git(main, "add", "tracked.gd")
        _git(main, "commit", "-q", "-m", "init")
        worktree = base / "wt-agent"
        _git(main, "worktree", "add", "-q", str(worktree), "-b", "feat/coplanar")
        (worktree / "qa").mkdir()
        (worktree / "qa" / "coplanar_check.gd").write_text("class_name CoplanarCheck\n", encoding="utf-8")
        return main, worktree

    def test_add_from_a_worktree_records_an_untracked_file_and_search_flags_it(self) -> None:
        main, worktree = self.make_repo()
        code, added, err = self.run_cli([
            "add", "--title", "CoplanarCheck merges coplanar faces", "--description", "Use it before merging.",
            "--source-repo", "BrickWild", "--source-file", "qa/coplanar_check.gd",
            "--hierarchy-path", "procedural/buildings", "--workspace", str(worktree),
        ])
        self.assertEqual(code, EXIT_OK, err)
        ref = added["source_ref"]
        self.assertEqual((ref["committed"], ref["status"], ref["branch"]), (False, "untracked", "feat/coplanar"))
        self.assertEqual((ref["repo"], ref["path"]), ("BrickWild", "qa/coplanar_check.gd"))
        self.assertEqual(Path(ref["worktreePath"]), worktree.resolve())
        self.assertTrue(ref["commit"])
        self.assertTrue(any("other agents will not have it" in note for note in added["notes"]), added)

        _, from_main, _ = self.run_cli(["search", "CoplanarCheck", "--workspace", str(main)])
        note = from_main["entries"][0]["source_note"]
        self.assertIn(f"uncommitted in worktree {worktree.resolve().as_posix()}", note)
        self.assertIn("qa/coplanar_check.gd is not in this workspace", note)
        _, got, _ = self.run_cli(["get", added["id"], "--workspace", str(main)])
        self.assertEqual(got["source_note"], note)
        self.assertEqual(got["source_ref"]["status"], "untracked")
        _, from_worktree, _ = self.run_cli(["search", "CoplanarCheck", "--workspace", str(worktree)])
        self.assertNotIn("not in this workspace", from_worktree["entries"][0]["source_note"])
        _, brief, _ = self.run_cli(["search", "CoplanarCheck", "--brief", "--workspace", str(main)])
        self.assertNotIn("source_note", brief["entries"][0])

    def test_committed_and_modified_files_and_update_refresh(self) -> None:
        main, _ = self.make_repo()
        ref = capture_source_ref("tracked.gd", main)
        self.assertEqual((ref["status"], ref["committed"], ref["branch"]), ("clean", True, "main"))
        (main / "tracked.gd").write_text("extends Node2D\n", encoding="utf-8")
        self.assertEqual(capture_source_ref("tracked.gd", main)["status"], "modified")
        self.assertEqual(capture_source_ref("nope.gd", main)["status"], "missing")

        entry_id = self.add_prose("Tracked file lesson", "d")
        code, revised, _ = self.run_cli(["update", entry_id, "--source-file", "tracked.gd", "--workspace", str(main)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(revised["source_ref"]["status"], "modified")
        self.assertIn("source_ref", revised["updated"])

    def test_add_outside_git_records_nothing_and_does_not_fail(self) -> None:
        outside = Path(tempfile.mkdtemp(prefix="kb-nogit-"))
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        if find_worktree_root(outside) is not None:
            self.skipTest("the system temp dir is inside a git checkout")
        (outside / "notes.py").write_text("x = 1\n", encoding="utf-8")
        code, added, err = self.run_cli([
            "add", "--title", "Outside git lesson", "--description", "d", "--source-repo", "scratch",
            "--source-file", "notes.py", "--hierarchy-path", "misc", "--workspace", str(outside),
        ])
        self.assertEqual((code, err), (EXIT_OK, ""))
        self.assertNotIn("source_ref", added)
        self.assertIsNone(self.open_store().get_entry(added["id"]).source_ref)

    def test_a_missing_git_binary_records_nothing(self) -> None:
        main, _ = self.make_repo()
        with patch("backend.knowledge.source_ref.subprocess.run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(capture_source_ref("tracked.gd", main))

    def test_repo_name_follows_a_linked_worktree_back_to_the_main_checkout(self) -> None:
        main, worktree = self.make_repo()
        self.assertEqual(repo_name(worktree), "BrickWild")
        self.assertEqual(repo_name(main), "BrickWild")
        reader = ReaderContext.for_workspace(worktree / "qa")
        self.assertEqual(reader.root, worktree)
        self.assertIn("brickwild", reader.repo_keys)


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class StderrEchoTests(KnowledgeCliTestCase):
    """KB-STDERR-001: advisory lines reach stderr only on a terminal; the JSON always has them."""

    TRACE = "I ran the exporter, then I tried the legacy flag. It worked."

    def _add(self, err_stream) -> dict:
        out = io.StringIO()
        with patch("backend.cli.knowledge.KnowledgeStore", self.open_store), \
                redirect_stdout(out), redirect_stderr(err_stream):
            code = dispatch(["knowledge", "add", "--title", "Exporter legacy flag", "--description",
                             self.TRACE, "--source-repo", "demo", "--hierarchy-path", "Tools/Export"])
        self.assertEqual(code, EXIT_OK)
        return json.loads(out.getvalue())

    def test_terminal_gets_the_human_echo(self) -> None:
        err = _Terminal()
        result = self._add(err)
        text = err.getvalue()
        self.assertIn("warning (reads-like-a-trace)", text)
        self.assertIn(f"saved anyway as {result['id']}", text)
        self.assertIn("note: hierarchy path normalized to lowercase: tools/export", text)

    def test_pipe_gets_nothing_and_the_json_carries_it_all(self) -> None:
        err = io.StringIO()
        result = self._add(err)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("hierarchy path normalized to lowercase: tools/export", result["notes"])
        self.assertTrue(any(w["kind"] == "reads_like_a_trace" for w in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
