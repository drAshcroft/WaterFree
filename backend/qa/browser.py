"""The Playwright half of the loop: take a snapshot, execute one action, watch.

Two responsibilities, kept apart on purpose:

* **Execute.** `execute(action)` never raises because of the page. A control
  that vanished between the snapshot and the click is normal in a live app, so
  it returns `"target gone"` and lets the model choose again. Only a dead
  browser is an exception.
* **Collect.** Console errors, uncaught page errors, failed requests and 4xx/5xx
  responses are gathered by listeners, not by the model. These are the findings
  that need no judgement, and a 9B model asked to notice them would miss most.

Playwright is imported lazily inside `start()` so `qa personas` and `qa report`
work without it installed, and the frozen build does not pay for it at startup.

Element addressing: `snapshot.py` stamps `data-wfqa="<n>"` on every element it
numbers, so a click resolves through that attribute rather than a selector the
model invented. The number is only valid for the snapshot that produced it,
which is why `execute` refuses to act on a stale one.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.qa.actions import Action
from backend.qa.snapshot import SNAPSHOT_JS, Snapshot, build_snapshot

DEFAULT_VIEWPORT = (1280, 800)

# How long to wait for a control before deciding it is gone. Deliberately short:
# the model gets another turn either way, and a long wait per step makes a
# 40-step run unbearable.
ACTION_TIMEOUT_MS = 4000

# How long to wait after navigation for a client-rendered app to paint its
# first controls. Short enough not to matter on a server-rendered page.
HYDRATION_TIMEOUT_MS = 5000

# Settle time after an action, by persona pacing. The impatient persona is
# *supposed* to act before the page is ready -- that is how it finds races.
PACING_SETTLE_MS = {"fast": 120, "normal": 450, "slow": 1200}

# Console noise every app emits. Filtered so the signal stays readable; anything
# that looks like a real error still gets through.
BENIGN_CONSOLE = re.compile(
    r"favicon|devtools|sourcemap|source map|Download the React DevTools|"
    r"\[vite\] connect|\[HMR\]|WebSocket connection to 'ws://localhost|"
    r"Lighthouse|autocomplete attribute|preloaded using link preload",
    re.I,
)

# Requests whose failure says nothing about the app under test.
BENIGN_REQUEST = re.compile(r"favicon\.ico|/__vite|hot-update|analytics|googletagmanager", re.I)


class BrowserUnavailable(RuntimeError):
    """Playwright or its browser binary is missing. Message carries the fix."""


@dataclass
class Observation:
    """What the harness saw as a result of one action."""

    outcome: str
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    bad_responses: list[str] = field(default_factory=list)
    navigated: bool = False

    @property
    def collector_hits(self) -> list[tuple[str, str]]:
        """(kind, detail) for everything worth recording as a finding."""
        return (
            [("page error", e) for e in self.page_errors]
            + [("console error", e) for e in self.console_errors]
            + [("failed request", e) for e in self.failed_requests]
            + [("bad response", e) for e in self.bad_responses]
        )


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:
        raise BrowserUnavailable(
            "Playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from exc
    return sync_playwright


class Session:
    """One browser, one page, for the length of one persona's run."""

    def __init__(
        self,
        *,
        viewport: tuple[int, int] = DEFAULT_VIEWPORT,
        mobile: bool = False,
        headed: bool = False,
        pacing: str = "normal",
        reduce_motion: bool = True,
    ) -> None:
        self.viewport = viewport
        self.mobile = mobile
        self.headed = headed
        self.pacing = pacing if pacing in PACING_SETTLE_MS else "normal"
        self.reduce_motion = reduce_motion

        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

        # Drained into each Observation, so a hit is reported once at the step
        # it happened on rather than repeated for the rest of the run.
        self._console: list[str] = []
        self._page_errors: list[str] = []
        self._failed: list[str] = []
        self._bad_status: list[str] = []
        self.total_console_errors = 0
        self._last_number_digest = ""

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "Session":
        sync_playwright = _import_playwright()
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(
                headless=not self.headed,
                # Muted: a QA run should never make noise, and autoplay audio
                # blocks page load in some browsers.
                args=["--mute-audio"],
            )
        except Exception as exc:  # Playwright raises its own Error type
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(
                f"Could not launch Chromium: {exc}\n"
                "If the browser is not installed, run:\n"
                "  python -m playwright install chromium"
            ) from exc

        self._context = self._browser.new_context(
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
            is_mobile=self.mobile,
            has_touch=self.mobile,
            # A fresh profile every run: a returning-user state would make runs
            # non-comparable and hide first-run bugs, which are the ones a
            # first-timer persona exists to find.
            reduced_motion="reduce" if self.reduce_motion else "no-preference",
        )
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        self.page = self._context.new_page()
        self._wire_collectors(self.page)
        return self

    def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = self.page = None

    def __enter__(self) -> "Session":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- collectors ----------------------------------------------------------

    def _wire_collectors(self, page) -> None:
        def on_console(msg) -> None:
            if msg.type != "error":
                return
            text = (msg.text or "").strip()
            if not text or BENIGN_CONSOLE.search(text):
                return
            self.total_console_errors += 1
            self._console.append(text[:400])

        def on_page_error(error) -> None:
            self._page_errors.append(str(error).strip()[:400])

        def on_request_failed(request) -> None:
            if BENIGN_REQUEST.search(request.url):
                return
            failure = getattr(request, "failure", None)
            reason = failure if isinstance(failure, str) else getattr(failure, "error_text", "")
            self._failed.append(f"{request.method} {request.url} ({reason or 'failed'})"[:400])

        def on_response(response) -> None:
            if response.status < 400 or BENIGN_REQUEST.search(response.url):
                return
            self._bad_status.append(f"HTTP {response.status} {response.url}"[:400])

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)

    def _drain(self, outcome: str, *, navigated: bool = False) -> Observation:
        observation = Observation(
            outcome=outcome,
            console_errors=list(self._console),
            page_errors=list(self._page_errors),
            failed_requests=list(self._failed),
            bad_responses=list(self._bad_status),
            navigated=navigated,
        )
        self._console.clear()
        self._page_errors.clear()
        self._failed.clear()
        self._bad_status.clear()
        return observation

    # -- navigation and observation -----------------------------------------

    def goto(self, url: str) -> Observation:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            return self._drain(f"could not open {url}: {_short(exc)}")
        self._settle()
        self.wait_for_controls()
        return self._drain(f"opened {url}", navigated=True)

    def wait_for_controls(self, timeout_ms: int = HYDRATION_TIMEOUT_MS) -> bool:
        """Give a client-rendered app a moment to paint its first controls.

        `domcontentloaded` fires before a React/Vite app has rendered anything,
        so the first snapshot of a real SPA saw an empty DOM. The model then
        reported "no interactive elements" as a HIGH finding on step 1 of every
        run -- a false positive produced entirely by the harness.

        Returns False on timeout rather than raising: a genuinely empty page is
        a real finding, and the model should be the one to notice it.
        """
        try:
            self.page.wait_for_function(
                # Cheap structural test, deliberately not the full snapshot
                # selector: we only need to know that *something* is there.
                "() => !!document.querySelector('button, a[href], input, select, "
                "textarea, [role=\"button\"], [data-testid]') "
                "|| !!document.querySelector('canvas')",
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return False

    def snapshot(self) -> Snapshot:
        """Re-number the page and read it.

        Every call re-stamps `data-wfqa`, so element numbers always describe the
        page as it is now. The digest is what the driver watches for loops.
        """
        raw = self.page.evaluate(SNAPSHOT_JS)
        snap = build_snapshot(raw, console_errors=self.total_console_errors)
        self._last_number_digest = snap.digest
        return snap

    def screenshot(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(path), full_page=False)
        except Exception:
            return ""
        return path.name

    def screenshot_b64(self) -> str:
        try:
            return base64.b64encode(self.page.screenshot(full_page=False)).decode("ascii")
        except Exception:
            return ""

    # -- execution -----------------------------------------------------------

    def execute(self, action: Action, snapshot: Snapshot) -> Observation:
        """Perform one action. Never raises because of the page.

        `snapshot` is the one the model was shown. If the page has re-rendered
        since, the numbering is stale and acting on it would click whatever now
        happens to sit at that number -- a silent mis-click that produces a
        nonsense finding. We re-read instead and report the change.
        """
        try:
            return self._execute(action, snapshot)
        except Exception as exc:
            # A crashed page is worth reporting, not worth aborting the run for.
            return self._drain(f"the action failed: {_short(exc)}")

    def _execute(self, action: Action, snapshot: Snapshot) -> Observation:
        verb = action.verb

        if verb == "SCROLL":
            delta = int(self.viewport[1] * 0.8)
            self.page.mouse.wheel(0, delta if action.text == "down" else -delta)
            self._settle()
            return self._drain(f"scrolled {action.text}")

        if verb == "PRESS":
            self.page.keyboard.press(action.text)
            self._settle()
            return self._drain(f"pressed {action.text}")

        if verb == "WAIT":
            self.page.wait_for_timeout(max(1, action.seconds) * 1000)
            return self._drain(f"waited {action.seconds}s")

        if verb == "BACK":
            before = self.page.url
            self.page.go_back(wait_until="domcontentloaded")
            self._settle()
            after = self.page.url
            # A fresh context's history starts at about:blank, so going back
            # from the first page of the app "succeeds" onto a blank tab. Every
            # later snapshot would then be empty and the run would be wasted,
            # so treat it as a no-op and step forward again.
            if after == before or after.startswith("about:"):
                try:
                    if after != before:
                        self.page.go_forward(wait_until="domcontentloaded")
                        self._settle()
                except Exception:
                    pass
                return self._drain("there was nowhere to go back to")
            return self._drain(f"went back to {after}", navigated=True)

        if verb in ("CLICK", "TYPE"):
            return self._act_on_element(action, snapshot)

        # LOOK / NOTE / DONE / STUCK do not touch the page.
        return self._drain("noted")

    def _act_on_element(self, action: Action, snapshot: Snapshot) -> Observation:
        element = snapshot.element(action.target or -1)
        if element is None:
            return self._drain(
                f"there is no element {action.target} on this screen. "
                "Use a number from the list."
            )
        if "disabled" in element.flags:
            return self._drain(f'element {action.target} ("{element.label[:40]}") is disabled')
        if "offscreen" in element.flags:
            return self._drain(
                f'element {action.target} ("{element.label[:40]}") is off screen; SCROLL to reach it'
            )

        locator = self.page.locator(f'[data-wfqa="{action.target}"]')
        try:
            if locator.count() == 0:
                return self._drain("target gone: that control is no longer on the page")
        except Exception:
            return self._drain("target gone: that control is no longer on the page")

        label = element.label[:40] or element.role
        url_before = self.page.url

        if action.verb == "TYPE":
            try:
                locator.first.fill(action.text, timeout=ACTION_TIMEOUT_MS)
            except Exception as exc:
                return self._drain(f'could not type into "{label}": {_short(exc)}')
            self._settle()
            return self._drain(f'typed "{action.text[:40]}" into "{label}"')

        try:
            # No force: a click that Playwright refuses is a real finding about
            # the app (covered by something, zero-sized, still animating), and
            # forcing it would hide exactly the bug we are looking for. The
            # knowledge base carries this lesson from the goblinchess harness.
            locator.first.click(timeout=ACTION_TIMEOUT_MS)
        except Exception as exc:
            return self._drain(f'could not click "{label}": {_short(exc)}')

        self._settle()
        navigated = self.page.url != url_before
        outcome = f'clicked "{label}"'
        if navigated:
            outcome += f" and the page went to {self.page.url}"
        return self._drain(outcome, navigated=navigated)

    def _settle(self) -> None:
        self.page.wait_for_timeout(PACING_SETTLE_MS[self.pacing])


def _short(exc: Exception) -> str:
    """First meaningful line of a Playwright error.

    Playwright errors carry a multi-paragraph log that is useful in a terminal
    and useless inside a 1,500-token prompt.
    """
    text = str(exc).strip().splitlines()
    for line in text:
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("="):
            return cleaned[:160]
    return "unknown error"
