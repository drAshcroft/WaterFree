"""
End-to-end tests for the Playwright session, against a real Chromium.

Everything else in the QA package is unit-tested with stubs, which is right for
the loop's logic but proves nothing about the half that touches a browser. This
module serves a fixture page over localhost and drives it for real: each action
verb, the "target gone" path, and the collectors.

Skipped, not failed, when Playwright or its browser is missing -- `qa doctor`
is where a missing browser is supposed to be reported.
"""

import threading
import unittest
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.qa.actions import parse
from backend.qa.browser import BrowserUnavailable, Session

FIXTURE = """<!doctype html>
<html><head><title>QA Fixture</title></head>
<body>
  <h1>Fixture</h1>
  <p>Some visible copy for the snapshot.</p>
  <button id="go">Start Game</button>
  <button id="off" disabled>Disabled Button</button>
  <input id="name" placeholder="Your name">
  <a href="/second.html">Go to second page</a>
  <button id="boom">Break it</button>
  <button id="vanish">Vanishing</button>
  <button id="swap">Swap</button>
  <div id="revealed" style="display:none"><button id="revealed-btn">Revealed</button></div>
  <div style="height:2000px"></div>
  <button id="low">Way Down Here</button>
  <script>
    document.getElementById('go').onclick = () => {
      document.getElementById('go').textContent = 'Started';
    };
    document.getElementById('boom').onclick = () => {
      console.error('TypeError: deliberate test error');
    };
    document.getElementById('vanish').onclick = (e) => { e.target.remove(); };
    document.getElementById('swap').onclick = () => {
      document.getElementById('swap').style.display = 'none';
      document.getElementById('revealed').style.display = 'block';
    };
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') document.title = 'Escaped';
    });
  </script>
</body></html>
"""

LATE = """<!doctype html>
<html><head><title>Late</title></head>
<body>
  <div id="root"></div>
  <script>
    // Renders after a tick, like a hydrating SPA. domcontentloaded fires first.
    setTimeout(() => {
      const b = document.createElement('button');
      b.textContent = 'Late Button';
      document.getElementById('root').appendChild(b);
    }, 700);
  </script>
</body></html>
"""

SECOND = """<!doctype html>
<html><head><title>Second</title></head>
<body><h1>Second page</h1><button id="back-target">On second page</button></body></html>
"""


def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True, args=["--mute-audio"]).close()
    except Exception:
        return False
    return True


AVAILABLE = _browser_available()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep the test output readable
        pass


@unittest.skipUnless(AVAILABLE, "Playwright or Chromium is not installed")
class BrowserSessionTests(unittest.TestCase):
    server = None
    thread = None
    tmp = None
    base = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "index.html").write_text(FIXTURE, encoding="utf-8")
        (root / "second.html").write_text(SECOND, encoding="utf-8")
        (root / "late.html").write_text(LATE, encoding="utf-8")
        (root / "empty.html").write_text(
            "<!doctype html><html><head><title>Empty</title></head>"
            "<body><p>Nothing to click here.</p></body></html>",
            encoding="utf-8",
        )

        handler = partial(_QuietHandler, directory=str(root))
        cls.server = HTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.session = Session(viewport=(1000, 700), pacing="fast").start()
        self.addCleanup(self.session.close)
        self.session.goto(f"{self.base}/index.html")

    # -- snapshot ------------------------------------------------------------

    def test_snapshot_numbers_the_controls_and_reads_the_text(self) -> None:
        snap = self.session.snapshot()
        labels = [e.label for e in snap.elements]
        self.assertIn("Start Game", labels)
        self.assertIn("Some visible copy for the snapshot.", snap.text)
        self.assertEqual(snap.title, "QA Fixture")
        self.assertTrue(snap.has_interactive)

    def test_disabled_and_offscreen_controls_are_flagged(self) -> None:
        snap = self.session.snapshot()
        disabled = next(e for e in snap.elements if e.label == "Disabled Button")
        self.assertIn("disabled", disabled.flags)

        far_down = next(e for e in snap.elements if e.label == "Way Down Here")
        self.assertIn("offscreen", far_down.flags)
        self.assertFalse(far_down.actionable)

    # -- actions -------------------------------------------------------------

    def _number_for(self, label: str, snap=None) -> int:
        snap = snap or self.session.snapshot()
        return next(e.number for e in snap.elements if e.label == label)

    def test_click_changes_the_page(self) -> None:
        snap = self.session.snapshot()
        action = parse(f"CLICK {self._number_for('Start Game', snap)}")
        observation = self.session.execute(action, snap)
        self.assertIn("clicked", observation.outcome)
        self.assertIn("Started", [e.label for e in self.session.snapshot().elements])

    def test_type_fills_an_input(self) -> None:
        snap = self.session.snapshot()
        number = next(e.number for e in snap.elements if e.role.startswith("input"))
        observation = self.session.execute(parse(f'TYPE {number} "Ada"'), snap)
        self.assertIn("typed", observation.outcome)
        self.assertEqual(
            self.session.page.locator("#name").input_value(), "Ada",
        )

    def test_clicking_a_disabled_control_is_refused_with_a_reason(self) -> None:
        snap = self.session.snapshot()
        observation = self.session.execute(
            parse(f"CLICK {self._number_for('Disabled Button', snap)}"), snap,
        )
        self.assertIn("disabled", observation.outcome)

    def test_offscreen_control_tells_the_model_to_scroll(self) -> None:
        snap = self.session.snapshot()
        observation = self.session.execute(
            parse(f"CLICK {self._number_for('Way Down Here', snap)}"), snap,
        )
        self.assertIn("SCROLL", observation.outcome)

    def test_a_number_that_does_not_exist_is_reported_not_raised(self) -> None:
        snap = self.session.snapshot()
        observation = self.session.execute(parse("CLICK 999"), snap)
        self.assertIn("no element 999", observation.outcome)

    def test_a_control_that_vanished_reports_target_gone(self) -> None:
        """The live-app case: the snapshot is a moment old by the time we act."""
        snap = self.session.snapshot()
        number = self._number_for("Vanishing", snap)
        self.session.page.locator("#vanish").evaluate("el => el.remove()")

        observation = self.session.execute(parse(f"CLICK {number}"), snap)
        self.assertIn("target gone", observation.outcome)

    def test_scroll_moves_the_page(self) -> None:
        snap = self.session.snapshot()
        self.session.execute(parse("SCROLL down"), snap)
        self.assertGreater(self.session.snapshot().scroll_y, 0)

    def test_press_reaches_the_page(self) -> None:
        snap = self.session.snapshot()
        self.session.execute(parse("PRESS Escape"), snap)
        self.assertEqual(self.session.snapshot().title, "Escaped")

    def test_wait_does_not_fail(self) -> None:
        snap = self.session.snapshot()
        observation = self.session.execute(parse("WAIT 1"), snap)
        self.assertIn("waited", observation.outcome)

    def test_navigation_then_back(self) -> None:
        snap = self.session.snapshot()
        self.session.execute(parse(f"CLICK {self._number_for('Go to second page', snap)}"), snap)
        self.assertEqual(self.session.snapshot().title, "Second")

        observation = self.session.execute(parse("BACK"), self.session.snapshot())
        self.assertIn("went back", observation.outcome)
        self.assertEqual(self.session.snapshot().title, "QA Fixture")

    def test_back_with_nowhere_to_go_is_reported_not_raised(self) -> None:
        observation = self.session.execute(parse("BACK"), self.session.snapshot())
        self.assertIn("nowhere to go back", observation.outcome)

    def test_hiding_a_control_releases_its_number(self) -> None:
        """A hidden element must not keep a stamp a visible one now owns.

        The stale-stamp bug: numbering cleared against every node the selector
        matched, including ones skipped for being hidden. A control that was
        hidden kept `data-wfqa="1"` while a newly visible control was given the
        same number, and the click resolved to the invisible one and timed out.
        Every show/hide app hit this.
        """
        snap = self.session.snapshot()
        self.session.execute(parse(f"CLICK {self._number_for('Swap', snap)}"), snap)

        after = self.session.snapshot()
        numbers = [e.number for e in after.elements]
        self.assertEqual(len(numbers), len(set(numbers)), "numbers must be unique")

        for element in after.elements:
            with self.subTest(number=element.number):
                self.assertEqual(
                    self.session.page.locator(f'[data-wfqa="{element.number}"]').count(),
                    1,
                    f"number {element.number} is stamped on more than one node",
                )

        revealed = self._number_for("Revealed", after)
        observation = self.session.execute(parse(f"CLICK {revealed}"), after)
        self.assertIn("clicked", observation.outcome)
        self.assertNotIn("could not click", observation.outcome)

    # -- collectors ----------------------------------------------------------

    def test_console_errors_are_collected(self) -> None:
        snap = self.session.snapshot()
        observation = self.session.execute(
            parse(f"CLICK {self._number_for('Break it', snap)}"), snap,
        )
        self.assertTrue(
            any("deliberate test error" in e for e in observation.console_errors),
            observation.console_errors,
        )
        self.assertEqual(
            [kind for kind, _ in observation.collector_hits], ["console error"],
        )

    def test_a_collector_hit_is_drained_and_not_repeated(self) -> None:
        snap = self.session.snapshot()
        self.session.execute(parse(f"CLICK {self._number_for('Break it', snap)}"), snap)
        follow_up = self.session.execute(parse("WAIT 1"), self.session.snapshot())
        self.assertEqual(follow_up.console_errors, [])

    def test_a_bad_response_is_collected(self) -> None:
        observation = self.session.goto(f"{self.base}/does-not-exist.html")
        self.assertTrue(
            any("404" in r for r in observation.bad_responses), observation.bad_responses,
        )

    def test_opening_a_dead_url_is_reported_not_raised(self) -> None:
        observation = self.session.goto("http://127.0.0.1:1/nothing")
        self.assertIn("could not open", observation.outcome)

    def test_goto_waits_for_a_client_rendered_app_to_paint(self) -> None:
        """domcontentloaded fires before an SPA has rendered anything.

        Without the wait, the first snapshot of a real app was empty and the
        model reported "no interactive elements" as a HIGH finding on step 1 of
        every run -- a false positive manufactured by the harness. Seen against
        goblinchess.
        """
        self.session.goto(f"{self.base}/late.html")
        snap = self.session.snapshot()

        self.assertTrue(snap.has_interactive, "the late-rendered button was missed")
        self.assertIn("Late Button", [e.label for e in snap.elements])

    def test_a_genuinely_empty_page_is_still_reported_as_empty(self) -> None:
        """The wait must not invent controls that are not there."""
        self.session.goto(f"{self.base}/empty.html")
        self.assertFalse(self.session.snapshot().has_interactive)

    # -- artefacts -----------------------------------------------------------

    def test_screenshots_are_written(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            name = self.session.screenshot(path)
        self.assertEqual(name, "shot.png")

    def test_screenshot_b64_is_produced_for_vision(self) -> None:
        self.assertTrue(self.session.screenshot_b64())


class MissingPlaywrightTests(unittest.TestCase):
    def test_the_error_names_the_install_commands(self) -> None:
        """A missing browser must be actionable, not a stack trace."""
        import backend.qa.browser as browser_module

        original = browser_module._import_playwright
        browser_module._import_playwright = lambda: (_ for _ in ()).throw(
            BrowserUnavailable(
                "Playwright is not installed. Run:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            )
        )
        try:
            with self.assertRaises(BrowserUnavailable) as ctx:
                Session().start()
        finally:
            browser_module._import_playwright = original

        self.assertIn("pip install playwright", str(ctx.exception))
        self.assertIn("playwright install chromium", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
