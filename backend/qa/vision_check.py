"""Does the screen agree with what the DOM says is on it?

The DOM is a claim, not a fact. An element can be present, sized and "visible"
to Playwright while being painted behind an overlay, drawn in white on white,
or never rendered at all because a canvas swallowed the frame. Those are the
failures a DOM-only harness reports as a clean pass.

So every few steps, and at every finding, the screenshot goes to the large
vision tier with the DOM's one-line claim and one question: does the picture
agree? Disagreement becomes a `note`-severity finding carrying both artefacts.
It is never used for navigation -- a 7B vision model is not reliable enough to
steer with, but it is reliable enough to say "that screen is blank".

Answers are constrained to agree / disagree / unsure. `unsure` is recorded and
never reported: a model that cannot tell is not evidence of a bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.llm import ollama_client
from backend.vision.models import LARGE, VisionModelMissing, ensure_available, resolve_model

AGREE = "agree"
DISAGREE = "disagree"
UNSURE = "unsure"

DEFAULT_EVERY = 5
TIMEOUT_SECONDS = 90
KEEP_ALIVE = "20m"

_SYSTEM = """You check whether a screenshot matches a description taken from the page's HTML.
You are not testing the app and you are not looking for bugs.
Answer on one line, in this exact shape:

VERDICT: agree | disagree | unsure
WHY: one short sentence

Say "disagree" only when the screenshot clearly contradicts the description --
for example the description names a dialog and the screen has none, or names
controls and the screen is blank or shows only an error.
Say "unsure" when the screenshot is too small, too dark, or too ambiguous to tell.
Minor differences in wording, styling or layout are "agree"."""

_VERDICT_RE = re.compile(r"\bVERDICT\s*[:\-]?\s*(agree|disagree|unsure)\b", re.I)
_WHY_RE = re.compile(r"\bWHY\s*[:\-]?\s*(.+)", re.I)
# Fallback for a model that ignores the format and just says the word.
_BARE_RE = re.compile(r"\b(agree|disagree|unsure)\b", re.I)


@dataclass(frozen=True)
class VisionVerdict:
    verdict: str
    why: str
    model: str
    raw: str = ""

    @property
    def disagrees(self) -> bool:
        return self.verdict == DISAGREE


def parse_verdict(reply: str, *, model: str = "") -> VisionVerdict:
    """Pull a verdict out of whatever the vision model said.

    Unparseable is `unsure`, not `disagree`: a confused reply must never become
    a reported finding.
    """
    text = (reply or "").strip()
    match = _VERDICT_RE.search(text)
    if not match:
        match = _BARE_RE.search(text)
    verdict = match.group(1).lower() if match else UNSURE

    why_match = _WHY_RE.search(text)
    why = why_match.group(1).strip() if why_match else ""
    if not why:
        # First non-empty line that is not the verdict line itself.
        for line in text.splitlines():
            cleaned = line.strip()
            if cleaned and not _VERDICT_RE.search(cleaned):
                why = cleaned
                break
    return VisionVerdict(verdict=verdict, why=why[:300], model=model, raw=text)


def check(
    screenshot_b64: str,
    claim: str,
    *,
    model: str = "",
    base: str = "",
    timeout: int = TIMEOUT_SECONDS,
) -> VisionVerdict:
    """Ask the vision model whether the screenshot matches the DOM's claim.

    Never raises: a vision failure must not end a QA run. An unreachable or
    missing model returns `unsure` with the reason, which is recorded in the
    log and reported in run.json but produces no finding.
    """
    if not screenshot_b64:
        return VisionVerdict(UNSURE, "no screenshot was captured", model)

    try:
        model_id = resolve_model(_purpose(), override=model, tier=LARGE)
        ensure_available(model_id, base=base)
    except (VisionModelMissing, Exception) as exc:  # noqa: BLE001 - reported, not raised
        return VisionVerdict(UNSURE, f"vision model unavailable: {exc}", model or "")

    kwargs = {"base": base} if base else {}
    try:
        answer = ollama_client.chat(
            model=model_id,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"The HTML says this screen is:\n{claim.strip()}\n\nDoes the screenshot agree?"},
            ],
            images=[screenshot_b64],
            timeout=timeout,
            keep_alive=KEEP_ALIVE,
            **kwargs,
        )
    except ollama_client.OllamaError as exc:
        return VisionVerdict(UNSURE, f"vision call failed: {exc}", model_id)

    return parse_verdict(answer, model=model_id)


def _purpose():
    """The vision purpose to resolve the model from.

    Imported through `get_purpose` so an env override of the LARGE tier applies
    here exactly as it does to `waterfree vision`.
    """
    from backend.vision.models import get_purpose  # noqa: PLC0415

    return get_purpose("triage")


def should_check(step: int, every: int) -> bool:
    """True on every `every`-th step. `every <= 0` disables vision entirely."""
    if every <= 0:
        return False
    return step % every == 0
