"""The loop: snapshot -> prompt -> model -> action -> execute -> record.

Bounded four ways, because an unbounded agent with a browser is a way to burn
an afternoon:

* step cap, wall-clock cap
* the model saying DONE or STUCK
* loop detection -- the same action on the same screen three times
* a parse budget -- a model that cannot emit a valid action is not testing
  anything, and the fallback tier gets one chance before the run ends

`STUCK` is a legitimate, reportable outcome, not a failure of the harness. A
tester who cannot work out what to do next has found something.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.llm import ollama_client
from backend.qa import models, vision_check
from backend.qa.actions import Action, ParseError, parse
from backend.qa.browser import Observation, Session
from backend.qa.findings import Finding, RunLog
from backend.qa.personas import Persona
from backend.qa.prompts import HistoryItem, RunBrief, system_prompt, turn_prompt
from backend.qa.snapshot import Snapshot

DEFAULT_STEPS = 40
DEFAULT_MINUTES = 10

# Three identical (action, screen) pairs means the model is not learning from
# the outcome. Two is too eager -- retrying once after a transient miss is
# reasonable behaviour.
LOOP_LIMIT = 3

# Consecutive unparseable replies before switching tiers, then giving up.
PARSE_BUDGET = 3

# Collector hits are deduplicated across the run: the same console error on
# every step is one bug, not forty findings.
COLLECTOR_SEVERITY = {
    "page error": "high",
    "failed request": "medium",
    "bad response": "medium",
    "console error": "medium",
}


@dataclass
class RunOutcome:
    reason: str                       # done | stuck | steps | time | loop | unparseable | error
    summary: str = ""
    steps: int = 0
    findings: list[Finding] = field(default_factory=list)
    model: str = ""
    vision_checks: int = 0
    vision_disagreements: int = 0

    def as_dict(self) -> dict:
        return {
            "reason": self.reason,
            "summary": self.summary,
            "steps": self.steps,
            "model": self.model,
            "visionChecks": self.vision_checks,
            "visionDisagreements": self.vision_disagreements,
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass
class Caps:
    steps: int = DEFAULT_STEPS
    minutes: int = DEFAULT_MINUTES
    vision_every: int = vision_check.DEFAULT_EVERY


class Driver:
    """Drives one persona through one goal against one URL."""

    def __init__(
        self,
        session: Session,
        run_log: RunLog,
        persona: Persona,
        brief: RunBrief,
        *,
        caps: Caps | None = None,
        model: str = "",
        base: str = "",
        chat=None,
    ) -> None:
        self.session = session
        self.log = run_log
        self.persona = persona
        self.brief = brief
        self.caps = caps or Caps()
        self.base = base
        # Injectable so the loop is testable without Ollama.
        self._chat = chat or self._ollama_chat
        self.model = model or models.resolve_model(tier=models.DEFAULT)
        self._tier = models.DEFAULT

        self._history: list[HistoryItem] = []
        # Fingerprints of findings already recorded, from every source.
        self._seen_collector: set[str] = set()
        self._loop_counts: dict[tuple[str, str], int] = {}
        self.vision_checks = 0
        self.vision_disagreements = 0

    # -- model ---------------------------------------------------------------

    def _ollama_chat(self, system: str, user: str) -> str:
        kwargs = {"base": self.base} if self.base else {}
        return ollama_client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            timeout=models.TIMEOUT_SECONDS,
            keep_alive=models.KEEP_ALIVE,
            options=models.OPTIONS,
            **kwargs,
        )

    # -- the loop ------------------------------------------------------------

    def run(self, url: str) -> RunOutcome:
        deadline = time.time() + self.caps.minutes * 60
        system = system_prompt(self.persona, self.brief)
        self.log.event("system_prompt", chars=len(system), model=self.model)

        observation = self.session.goto(url)
        self.log.event("goto", url=url, outcome=observation.outcome)
        self._record_collectors(observation, step=0)

        previous: Snapshot | None = None
        parse_failures = 0
        error_note = ""

        for step in range(1, self.caps.steps + 1):
            if time.time() > deadline:
                return self._finish("time", f"ran out of time after {step - 1} steps", step - 1)

            try:
                snapshot = self.session.snapshot()
            except Exception as exc:
                return self._finish("error", f"could not read the page: {exc}", step - 1)

            shot = self.log.next_shot_path(f"step{step:02d}")
            screenshot_name = self.session.screenshot(shot)

            user = turn_prompt(
                self.brief, snapshot, self._history,
                previous=previous, step=step,
                steps_left=self.caps.steps - step,
                error=error_note,
            )
            error_note = ""

            try:
                reply = self._chat(system, user)
            except ollama_client.OllamaError as exc:
                return self._finish("error", f"the driver model failed: {exc}", step - 1)

            try:
                action = parse(reply)
                parse_failures = 0
            except ParseError as exc:
                parse_failures += 1
                self.log.event("parse_error", step=step, reply=reply[:400], message=str(exc)[:200])
                if parse_failures >= PARSE_BUDGET:
                    switched = self._try_fallback_tier()
                    if not switched:
                        return self._finish(
                            "unparseable",
                            f"the model stopped producing valid actions after {step} steps",
                            step,
                        )
                    parse_failures = 0
                error_note = str(exc)
                continue

            # Loop detection before execution: repeating a no-op action on an
            # unchanged screen is the classic small-model failure, and it is
            # cheaper to stop than to spend the remaining budget on it.
            key = (action.describe(), snapshot.digest)
            self._loop_counts[key] = self._loop_counts.get(key, 0) + 1
            if self._loop_counts[key] >= LOOP_LIMIT and not action.is_terminal:
                return self._finish(
                    "loop",
                    f"repeated `{action.describe()}` on an unchanged screen {LOOP_LIMIT} times",
                    step,
                )

            if action.verb == "NOTE":
                self._add_model_finding(action, step, snapshot, screenshot_name)
                self._remember(step, action, "recorded")
                self._vision_for_finding(step, snapshot, screenshot_name)
                previous = snapshot
                continue

            if action.verb == "LOOK":
                answer = self._look(action, snapshot, step)
                self._remember(step, action, answer)
                previous = snapshot
                continue

            if action.is_terminal:
                reason = "done" if action.verb == "DONE" else "stuck"
                self.log.event("step", step=step, action=action.describe(), outcome=reason)
                return self._finish(reason, action.text, step)

            observation = self.session.execute(action, snapshot)
            self.log.record_action(action.describe())
            self.log.event(
                "step", step=step,
                action=action.describe(),
                outcome=observation.outcome,
                digest=snapshot.digest,
                url=snapshot.url,
                screenshot=screenshot_name,
            )
            self._record_collectors(observation, step=step)
            self._remember(step, action, observation.outcome)

            if vision_check.should_check(step, self.caps.vision_every):
                self._vision(step, snapshot, screenshot_name, routine=True)

            previous = snapshot

        return self._finish("steps", f"used the whole {self.caps.steps}-step budget", self.caps.steps)

    # -- helpers -------------------------------------------------------------

    def _remember(self, step: int, action: Action, outcome: str) -> None:
        self._history.append(HistoryItem(step=step, action=action.describe(), outcome=outcome[:160]))

    def _try_fallback_tier(self) -> bool:
        """Switch to the steadier model once. Returns False if already there."""
        if self._tier == models.FALLBACK:
            return False
        self._tier = models.FALLBACK
        self.model = models.resolve_model(tier=models.FALLBACK)
        self.log.event("model_switch", tier=models.FALLBACK, model=self.model)
        return True

    def _add_model_finding(
        self, action: Action, step: int, snapshot: Snapshot, screenshot: str,
    ) -> Finding | None:
        """Record a NOTE, unless it is one we already have.

        A model that cannot get past a screen tends to re-report the same
        problem every turn. Loop detection stops the run a few steps later, but
        without this the report would carry three identical findings and a
        reader would think it happened three times. Collector findings are
        deduplicated the same way, for the same reason.
        """
        severity = action.severity or "note"
        # A small model often pours the whole explanation into the title slot.
        # The detail field keeps it; the title has to stay scannable in a
        # report table, so it is capped rather than trusted.
        title = _trim_title(action.text or "(untitled)")
        fingerprint = f"model:{severity}:{title.lower()}"
        if fingerprint in self._seen_collector:
            self.log.event("duplicate_note", step=step, title=title)
            return None
        self._seen_collector.add(fingerprint)

        return self.log.add_finding(Finding(
            severity=severity,
            title=title,
            detail=action.detail,
            step=step,
            url=snapshot.url,
            screenshot=screenshot,
            source="model",
        ))

    def _record_collectors(self, observation: Observation, *, step: int) -> None:
        """Turn harness-observed errors into findings, once each."""
        for kind, detail in observation.collector_hits:
            fingerprint = f"{kind}:{detail[:160]}"
            if fingerprint in self._seen_collector:
                continue
            self._seen_collector.add(fingerprint)
            self.log.add_finding(Finding(
                severity=COLLECTOR_SEVERITY.get(kind, "low"),
                title=f"{kind}: {detail[:80]}",
                detail=detail,
                step=step,
                url=self.session.page.url if self.session.page else "",
                source="collector",
            ))

    def _look(self, action: Action, snapshot: Snapshot, step: int) -> str:
        """The model asked to see the screen. Answer from the vision model."""
        b64 = self.session.screenshot_b64()
        verdict = vision_check.check(
            b64,
            f"{snapshot.claim()}\n\nThe tester asks: {action.text}",
            base=self.base,
        )
        self.log.event("look", step=step, question=action.text, answer=verdict.why)
        return verdict.why or "could not see the screen"

    def _vision(self, step: int, snapshot: Snapshot, screenshot: str, *, routine: bool) -> None:
        b64 = self.session.screenshot_b64()
        verdict = vision_check.check(b64, snapshot.claim(), base=self.base)
        self.vision_checks += 1
        self.log.event(
            "vision", step=step, verdict=verdict.verdict, why=verdict.why, model=verdict.model,
        )
        if not verdict.disagrees:
            return
        self.vision_disagreements += 1
        self.log.add_finding(Finding(
            severity="note",
            title="screen does not match the page structure",
            detail=(
                f"The HTML says: {snapshot.claim()}\n"
                f"The screenshot reportedly shows: {verdict.why}"
            ),
            step=step,
            url=snapshot.url,
            screenshot=screenshot,
            source="vision",
        ))

    def _vision_for_finding(self, step: int, snapshot: Snapshot, screenshot: str) -> None:
        if self.caps.vision_every <= 0:
            return
        self._vision(step, snapshot, screenshot, routine=False)

    def _finish(self, reason: str, summary: str, steps: int) -> RunOutcome:
        self.log.event("finish", reason=reason, summary=summary, steps=steps)
        return RunOutcome(
            reason=reason,
            summary=summary,
            steps=steps,
            findings=list(self.log.findings),
            model=self.model,
            vision_checks=self.vision_checks,
            vision_disagreements=self.vision_disagreements,
        )


MAX_TITLE_CHARS = 90


def _trim_title(text: str) -> str:
    """One scannable line. Cuts at a sentence end when there is one nearby."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= MAX_TITLE_CHARS:
        return cleaned
    head = cleaned[:MAX_TITLE_CHARS]
    stop = head.rfind(". ")
    if stop > MAX_TITLE_CHARS // 2:
        return head[:stop]
    return head.rstrip() + "…"


def run_persona(
    url: str,
    persona: Persona,
    brief: RunBrief,
    run_dir: Path,
    *,
    caps: Caps | None = None,
    model: str = "",
    base: str = "",
    headed: bool = False,
) -> RunOutcome:
    """One persona, start to finish, with its own browser and run directory."""
    log = RunLog(run_dir)
    caps = caps or Caps()
    session = Session(
        viewport=persona.viewport,
        mobile=persona.mobile,
        headed=headed,
        pacing=persona.pacing,
    )
    with session:
        driver = Driver(session, log, persona, brief, caps=caps, model=model, base=base)
        outcome = driver.run(url)

    log.write_run_json(
        url=url,
        goal=brief.goal,
        hints=brief.hints,
        persona=persona.as_dict(),
        model=outcome.model,
        caps={"steps": caps.steps, "minutes": caps.minutes, "visionEvery": caps.vision_every},
        outcome={"reason": outcome.reason, "summary": outcome.summary, "steps": outcome.steps},
        vision={"checks": outcome.vision_checks, "disagreements": outcome.vision_disagreements},
    )
    log.write_findings_md(
        title=f"QA run: {persona.name} - {brief.goal[:60]}",
        meta_lines=[
            f"- URL: {url}",
            f"- Persona: {persona.name} ({persona.summary})",
            f"- Model: {outcome.model}",
            f"- Ended: {outcome.reason} after {outcome.steps} steps",
        ],
        summary=outcome.summary,
    )
    return outcome
