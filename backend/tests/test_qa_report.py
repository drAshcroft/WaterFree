"""
Tests for vision-verdict parsing and the consolidated report.

The vision tests matter because of the failure direction: a confused vision
reply must never become a reported bug. Everything unparseable is `unsure`.

The report tests cover cross-run grouping -- a finding two personas trip over
independently is the cheapest corroboration a sweep produces, and it has to
survive the two of them wording it differently.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.qa.findings import Finding, RunLog
from backend.qa.report import collect, find_run_dirs, render
from backend.qa.vision_check import (
    AGREE,
    DISAGREE,
    UNSURE,
    parse_verdict,
    should_check,
)


class VerdictParsingTests(unittest.TestCase):
    def test_parses_the_requested_shape(self) -> None:
        verdict = parse_verdict("VERDICT: disagree\nWHY: the screen is blank")
        self.assertEqual(verdict.verdict, DISAGREE)
        self.assertEqual(verdict.why, "the screen is blank")
        self.assertTrue(verdict.disagrees)

    def test_parses_a_bare_word(self) -> None:
        self.assertEqual(parse_verdict("I agree with the description.").verdict, AGREE)

    def test_unparseable_is_unsure_not_disagree(self) -> None:
        """A confused reply must never become a finding."""
        self.assertEqual(parse_verdict("").verdict, UNSURE)
        self.assertEqual(parse_verdict("asdfghjkl").verdict, UNSURE)
        self.assertFalse(parse_verdict("asdfghjkl").disagrees)

    def test_unsure_does_not_disagree(self) -> None:
        self.assertFalse(parse_verdict("VERDICT: unsure\nWHY: too dark").disagrees)

    def test_falls_back_to_the_first_line_for_the_reason(self) -> None:
        verdict = parse_verdict("The page looks empty to me.\nVERDICT: disagree")
        self.assertEqual(verdict.verdict, DISAGREE)
        self.assertIn("empty", verdict.why)


class VisionCadenceTests(unittest.TestCase):
    def test_checks_every_nth_step(self) -> None:
        self.assertTrue(should_check(5, 5))
        self.assertTrue(should_check(10, 5))
        self.assertFalse(should_check(4, 5))

    def test_zero_disables_vision(self) -> None:
        self.assertFalse(should_check(5, 0))
        self.assertFalse(should_check(100, 0))


def _make_run(root: Path, persona: str, findings: list[Finding], *, goal="play") -> Path:
    run_dir = root / f"{persona}-1"
    log = RunLog(run_dir)
    for finding in findings:
        log.add_finding(finding)
    log.write_run_json(
        url="http://localhost:5173",
        goal=goal,
        persona={"name": persona, "viewport": "1280x800"},
        model="qwen3.5:9b",
        outcome={"reason": "done", "summary": "finished", "steps": 12},
    )
    return run_dir


class ReportGroupingTests(unittest.TestCase):
    def test_similar_findings_from_two_personas_become_one_group(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root, "first-timer", [
                Finding("high", "Start button does nothing", "clicked it", 3),
            ])
            _make_run(root, "impatient", [
                Finding("high", "the Start button does nothing at all", "clicked twice", 2),
            ])
            groups, meta = collect(find_run_dirs([str(root)]))

        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0].runs), ["first-timer", "impatient"])
        self.assertEqual(len(meta), 2)

    def test_different_findings_stay_separate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root, "careful", [
                Finding("high", "Start button does nothing", "a", 1),
                Finding("high", "score counter shows NaN after a win", "b", 4),
            ])
            groups, _ = collect(find_run_dirs([str(root)]))
        self.assertEqual(len(groups), 2)

    def test_findings_of_different_severity_are_never_merged(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root, "a", [Finding("high", "Start button does nothing", "x", 1)])
            _make_run(root, "b", [Finding("low", "Start button does nothing", "x", 1)])
            groups, _ = collect(find_run_dirs([str(root)]))
        self.assertEqual(len(groups), 2)

    def test_corroborated_findings_sort_above_single_sightings(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root, "a", [
                Finding("high", "lonely problem here", "x", 1),
                Finding("high", "shared problem here", "x", 2),
            ])
            _make_run(root, "b", [Finding("high", "shared problem here too", "x", 2)])
            groups, _ = collect(find_run_dirs([str(root)]))
        self.assertEqual(len(groups[0].runs), 2)

    def test_confirmed_sorts_above_unreproduced(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shaky = Finding("high", "flaky thing happens", "x", 1)
            shaky.status = "unreproduced"
            solid = Finding("high", "solid thing happens", "x", 2)
            solid.status = "confirmed"
            _make_run(root, "a", [shaky, solid])
            groups, _ = collect(find_run_dirs([str(root)]))
        self.assertEqual(groups[0].best_status, "confirmed")


class ReportRenderTests(unittest.TestCase):
    def test_renders_severity_sections_and_run_table(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root, "first-timer", [
                Finding("high", "Start button does nothing", "detail here", 3),
                Finding("low", "button label is lowercase", "nit", 5),
            ])
            markdown = render(find_run_dirs([str(root)]))

        self.assertIn("### High", markdown)
        self.assertIn("### Low", markdown)
        self.assertIn("first-timer", markdown)
        self.assertIn("detail here", markdown)

    def test_empty_sweep_says_so_rather_than_rendering_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root, "careful", [])
            markdown = render(find_run_dirs([str(root)]))
        self.assertIn("No findings", markdown)


class RunDirDiscoveryTests(unittest.TestCase):
    def test_accepts_a_run_dir_directly(self) -> None:
        with TemporaryDirectory() as tmp:
            run_dir = _make_run(Path(tmp), "a", [])
            self.assertEqual(find_run_dirs([str(run_dir)]), [run_dir])

    def test_accepts_a_parent_holding_several(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_run(root, "a", [])
            _make_run(root, "b", [])
            self.assertEqual(len(find_run_dirs([str(root)])), 2)

    def test_deduplicates(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = _make_run(root, "a", [])
            found = find_run_dirs([str(root), str(run_dir)])
        self.assertEqual(len(found), 1)

    def test_accepts_the_runs_root_holding_several_sweeps(self) -> None:
        """What a caller naturally passes: the --out they gave `qa run`."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sweep = root / "app-2026-09-18"
            _make_run(sweep, "first-timer", [])
            _make_run(sweep, "impatient", [])
            self.assertEqual(len(find_run_dirs([str(root)])), 2)

    def test_a_directory_with_no_runs_yields_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(find_run_dirs([tmp]), [])


if __name__ == "__main__":
    unittest.main()
