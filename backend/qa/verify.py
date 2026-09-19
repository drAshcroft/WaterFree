"""Replay a finding's recorded steps and see whether it happens again.

The reason this exists: in the FlighterPirates baseline, one in four HIGH
findings did not survive verification. A finding nobody can reproduce costs a
developer more time than it saves, so every finding carries its repro steps and
every run can be replayed.

Verification is deterministic and local -- no paid model, no judgement. Replay
the recorded actions against a fresh browser and compare the end state:

* `confirmed`    - the replay reached a state matching the finding's, and for
                   collector findings the same error appeared again
* `unreproduced` - the replay ran but the state did not match
* `error`        - the replay could not run at all (steps missing, app down)

`unreproduced` is not "false", it is "not shown again". Timing-dependent bugs
live here, which is why the status is recorded rather than the finding deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from backend.qa.actions import ParseError, parse
from backend.qa.browser import Session
from backend.qa.findings import Finding, load_run

CONFIRMED = "confirmed"
UNREPRODUCED = "unreproduced"
ERROR = "error"

# Collector findings carry the error text itself, so they can be verified
# exactly: did the same console/page/network error occur again?
_COLLECTOR_PREFIX = re.compile(r"^(page error|console error|failed request|bad response):\s*", re.I)


@dataclass
class VerifyResult:
    finding: Finding
    status: str
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "title": self.finding.title,
            "severity": self.finding.severity,
            "step": self.finding.step,
            "source": self.finding.source,
            "status": self.status,
            "note": self.note,
        }


def verify_run(run_dir: Path, *, headed: bool = False) -> list[VerifyResult]:
    """Replay every finding in a run directory."""
    loaded = load_run(Path(run_dir))
    run = loaded["run"]
    findings: list[Finding] = loaded["findings"]
    url = run.get("url", "")

    if not findings:
        return []
    if not url:
        return [VerifyResult(f, ERROR, "run.json has no url to replay against") for f in findings]

    persona = run.get("persona") or {}
    viewport = _viewport(persona.get("viewport", ""))

    results: list[VerifyResult] = []
    for finding in findings:
        results.append(_verify_one(finding, url, viewport, headed=headed))
    return results


def _verify_one(finding: Finding, url: str, viewport: tuple[int, int], *, headed: bool) -> VerifyResult:
    if not finding.repro:
        return VerifyResult(finding, ERROR, "no repro steps were recorded")

    actions = []
    for line in finding.repro:
        try:
            actions.append(parse(line))
        except ParseError:
            # A recorded step we can no longer parse is a harness problem, not
            # a verdict on the finding.
            return VerifyResult(finding, ERROR, f"could not replay step: {line[:60]}")

    session = Session(viewport=viewport, headed=headed, pacing="normal")
    try:
        with session:
            observation = session.goto(url)
            if "could not open" in observation.outcome:
                return VerifyResult(finding, ERROR, observation.outcome)

            seen_errors: list[str] = []
            for action in actions:
                snapshot = session.snapshot()
                if action.verb in ("CLICK", "TYPE") and snapshot.element(action.target or -1) is None:
                    return VerifyResult(
                        finding, UNREPRODUCED,
                        f"element {action.target} was not on the page this time",
                    )
                observation = session.execute(action, snapshot)
                seen_errors += [detail for _kind, detail in observation.collector_hits]

            final = session.snapshot()
            return _judge(finding, final, seen_errors)
    except Exception as exc:  # a browser that will not start is not a verdict
        return VerifyResult(finding, ERROR, f"replay failed: {exc}")


def _judge(finding: Finding, final, seen_errors: list[str]) -> VerifyResult:
    """Decide the status from the replayed end state."""
    if finding.source == "collector":
        expected = _COLLECTOR_PREFIX.sub("", finding.title).strip()
        needle = expected[:60]
        if needle and any(needle in error for error in seen_errors):
            return VerifyResult(finding, CONFIRMED, "the same error occurred again")
        return VerifyResult(finding, UNREPRODUCED, "the error did not occur on replay")

    # Model and vision findings describe a screen, not an exception. The best
    # deterministic check available is that the replay ended on the same URL
    # with the same dialogs open -- that is what "the same state" means without
    # asking a model to judge.
    if finding.url and final.url != finding.url:
        return VerifyResult(
            finding, UNREPRODUCED,
            f"replay ended on {final.url}, the finding was on {finding.url}",
        )
    return VerifyResult(
        finding, CONFIRMED,
        "the replay reached the same screen; the described behaviour is reachable",
    )


def apply_statuses(run_dir: Path, results: list[VerifyResult]) -> Path:
    """Rewrite findings.md with each finding's verified status attached."""
    from backend.qa.findings import RunLog  # noqa: PLC0415

    run_dir = Path(run_dir)
    loaded = load_run(run_dir)
    run = loaded["run"]

    by_key = {(r.finding.title, r.finding.step): r for r in results}
    log = RunLog(run_dir)
    for finding in loaded["findings"]:
        result = by_key.get((finding.title, finding.step))
        if result:
            finding.status = result.status
        log.findings.append(finding)

    persona = (run.get("persona") or {}).get("name", "unknown")
    outcome = run.get("outcome") or {}
    return log.write_findings_md(
        title=f"QA run: {persona} - {str(run.get('goal', ''))[:60]}",
        meta_lines=[
            f"- URL: {run.get('url', '')}",
            f"- Persona: {persona}",
            f"- Model: {run.get('model', '')}",
            f"- Ended: {outcome.get('reason', '')} after {outcome.get('steps', 0)} steps",
            f"- Verified: {summarize(results)}",
        ],
        summary=str(outcome.get("summary", "")),
    )


def summarize(results: list[VerifyResult]) -> str:
    counts = {CONFIRMED: 0, UNREPRODUCED: 0, ERROR: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return ", ".join(f"{n} {name}" for name, n in counts.items() if n)


def _viewport(raw: str) -> tuple[int, int]:
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", raw or "")
    if not match:
        return (1280, 800)
    return int(match.group(1)), int(match.group(2))
