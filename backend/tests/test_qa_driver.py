"""
Tests for the QA driver loop.

The browser and the model are both stubbed: the loop's job is to decide when to
stop, when to record, and when to give up, and none of that needs Chromium or
Ollama. The stop conditions are the important part -- an unbounded agent with a
browser is how a run burns an afternoon.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.qa.browser import Observation
from backend.qa.driver import LOOP_LIMIT, PARSE_BUDGET, Caps, Driver
from backend.qa.findings import RunLog
from backend.qa.personas import Persona
from backend.qa.prompts import RunBrief
from backend.qa.snapshot import build_snapshot

PERSONA = Persona(
    name="test-persona",
    summary="a stub",
    body="You are testing.",
    viewport=(1280, 800),
)

BRIEF = RunBrief(goal="reach the end")


def _raw_page(*, url="http://localhost/app", label="Start", title="App") -> dict:
    return {
        "url": url,
        "title": title,
        "viewport": [1280, 800],
        "overflow": [0, 0],
        "scrollY": 0,
        "dialogs": [],
        "focused": None,
        "canvases": 0,
        "text": "Welcome",
        "rows": [
            {"role": "button", "label": label, "testid": "", "value": "",
             "rect": [10, 10, 100, 30], "flags": []},
        ],
    }


class FakeSession:
    """Stands in for a Playwright Session. Records what it was asked to do."""

    def __init__(self, pages=None):
        self._pages = pages or [_raw_page()]
        self._index = 0
        self.page = type("P", (), {"url": "http://localhost/app"})()
        self.executed = []
        self.observations = []

    def goto(self, url):
        return Observation(outcome=f"opened {url}", navigated=True)

    def snapshot(self):
        raw = self._pages[min(self._index, len(self._pages) - 1)]
        return build_snapshot(raw)

    def screenshot(self, path: Path) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"")
        return Path(path).name

    def screenshot_b64(self) -> str:
        return ""

    def execute(self, action, snapshot):
        self.executed.append(action.describe())
        self._index += 1
        if self.observations:
            return self.observations.pop(0)
        return Observation(outcome=f"did {action.verb}")


def _driver(tmp, replies, session=None, caps=None):
    """A Driver whose model returns `replies` in order."""
    queue = list(replies)

    def chat(system, user):
        return queue.pop(0) if queue else 'STUCK "out of replies"'

    return Driver(
        session or FakeSession(),
        RunLog(Path(tmp) / "run"),
        PERSONA,
        BRIEF,
        # Vision off: it is exercised in its own tests and would need Ollama.
        caps=caps or Caps(steps=10, minutes=5, vision_every=0),
        chat=chat,
    )


class StopConditionTests(unittest.TestCase):
    def test_done_ends_the_run_with_its_summary(self) -> None:
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ['CLICK 1', 'DONE "reached the end"'])
            outcome = driver.run("http://localhost/app")
        self.assertEqual(outcome.reason, "done")
        self.assertEqual(outcome.summary, "reached the end")
        self.assertEqual(outcome.steps, 2)

    def test_stuck_is_a_valid_outcome_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ['STUCK "no idea what to click"'])
            outcome = driver.run("http://localhost/app")
        self.assertEqual(outcome.reason, "stuck")
        self.assertIn("no idea", outcome.summary)

    def test_step_cap_ends_the_run(self) -> None:
        # A screen that changes every step, so loop detection stays quiet and
        # the cap is what actually stops the run.
        session = FakeSession(pages=[_raw_page(label=f"Step {i}") for i in range(6)])
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ["CLICK 1"] * 20, session=session,
                             caps=Caps(steps=3, minutes=5, vision_every=0))
            outcome = driver.run("http://localhost/app")
        self.assertEqual(outcome.reason, "steps")
        self.assertEqual(outcome.steps, 3)

    def test_repeating_one_action_on_an_unchanged_screen_stops_the_run(self) -> None:
        """The classic small-model failure: click, nothing happens, click again."""
        session = FakeSession(pages=[_raw_page()])
        # Every snapshot is identical, so the digest never changes.
        session._pages = [_raw_page()]
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ["CLICK 1"] * 10, session=session,
                             caps=Caps(steps=10, minutes=5, vision_every=0))
            outcome = driver.run("http://localhost/app")
        self.assertEqual(outcome.reason, "loop")
        self.assertLessEqual(outcome.steps, LOOP_LIMIT)


class ParseBudgetTests(unittest.TestCase):
    def test_garbage_is_fed_back_before_it_counts_against_the_run(self) -> None:
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ["I think I should click the button", 'DONE "ok"'])
            outcome = driver.run("http://localhost/app")
        self.assertEqual(outcome.reason, "done")

    def test_persistent_garbage_switches_tier_then_ends_the_run(self) -> None:
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ["nonsense"] * 30,
                             caps=Caps(steps=30, minutes=5, vision_every=0))
            outcome = driver.run("http://localhost/app")
        self.assertEqual(outcome.reason, "unparseable")
        # One full budget on each tier before giving up.
        self.assertGreaterEqual(outcome.steps, PARSE_BUDGET * 2)


class FindingTests(unittest.TestCase):
    def test_note_becomes_a_finding_with_severity_and_detail(self) -> None:
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, [
                'NOTE high "Start button does nothing" :: "clicked Start, nothing happened"',
                'DONE "finished"',
            ])
            outcome = driver.run("http://localhost/app")
        self.assertEqual(len(outcome.findings), 1)
        finding = outcome.findings[0]
        self.assertEqual(finding.severity, "high")
        self.assertEqual(finding.source, "model")
        self.assertIn("nothing happened", finding.detail)

    def test_console_errors_are_recorded_without_the_model(self) -> None:
        session = FakeSession()
        session.observations = [
            Observation(outcome="clicked", console_errors=["TypeError: x is not a function"]),
        ]
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ["CLICK 1", 'DONE "done"'], session=session)
            outcome = driver.run("http://localhost/app")
        collector = [f for f in outcome.findings if f.source == "collector"]
        self.assertEqual(len(collector), 1)
        self.assertIn("TypeError", collector[0].detail)

    def test_the_same_console_error_is_reported_once(self) -> None:
        """One bug, not one finding per step."""
        session = FakeSession()
        repeated = "TypeError: x is not a function"
        session.observations = [
            Observation(outcome="clicked", console_errors=[repeated]),
            Observation(outcome="clicked", console_errors=[repeated]),
            Observation(outcome="clicked", console_errors=[repeated]),
        ]
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ["CLICK 1", "CLICK 1", "CLICK 1", 'DONE "done"'],
                             session=session)
            outcome = driver.run("http://localhost/app")
        collector = [f for f in outcome.findings if f.source == "collector"]
        self.assertEqual(len(collector), 1)

    def test_a_page_error_outranks_a_console_error(self) -> None:
        session = FakeSession()
        session.observations = [Observation(outcome="clicked", page_errors=["Uncaught boom"])]
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ["CLICK 1", 'DONE "done"'], session=session)
            outcome = driver.run("http://localhost/app")
        self.assertEqual(outcome.findings[0].severity, "high")


class ArtefactTests(unittest.TestCase):
    def test_the_run_directory_gets_a_log_and_screenshots(self) -> None:
        with TemporaryDirectory() as tmp:
            driver = _driver(tmp, ["CLICK 1", 'DONE "done"'])
            driver.run("http://localhost/app")
            run_dir = Path(tmp) / "run"
            self.assertTrue((run_dir / "log.jsonl").is_file())
            self.assertTrue(any((run_dir / "shots").iterdir()))


if __name__ == "__main__":
    unittest.main()
