"""Findings, the step log, and the run directory layout.

Layout matches the FlighterPirates baseline so its readers keep working:

    <out>/<slug>-<date>/<persona>-<n>/
        run.json        goal, hints, persona, model, caps, summary
        log.jsonl       one line per event (step, action, outcome, collector hits)
        findings.md     severity sections with repro steps and screenshot refs
        shots/NN-label.png
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SEVERITY_ORDER = ("high", "medium", "low", "note")


def slugify(text: str, *, fallback: str = "run") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or fallback


def slug_from_url(url: str) -> str:
    m = re.match(r"^\w+://([^/:?#]+)(?::(\d+))?(/[^?#]*)?", url.strip())
    if not m:
        return slugify(url)
    host, port, path = m.group(1), m.group(2), (m.group(3) or "").strip("/")
    bits = [host.replace("www.", "")]
    if port and host in ("localhost", "127.0.0.1"):
        bits.append(port)
    if path:
        bits.append(path.split("/")[0])
    return slugify("-".join(bits))


@dataclass
class Finding:
    severity: str
    title: str
    detail: str
    step: int
    url: str = ""
    screenshot: str = ""
    source: str = "model"          # model | collector | vision
    repro: list[str] = field(default_factory=list)
    status: str = ""               # filled by verify: confirmed | unreproduced | error

    def as_dict(self) -> dict:
        return asdict(self)


class RunLog:
    """Owns one run directory. Append-only; safe to read while running."""

    def __init__(self, run_dir: Path):
        self.dir = Path(run_dir)
        self.shots = self.dir / "shots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.shots.mkdir(exist_ok=True)
        self._log = self.dir / "log.jsonl"
        self._t0 = time.time()
        self._shot_index = 0
        self.findings: list[Finding] = []
        self.actions: list[str] = []

    # -- events ------------------------------------------------------------
    def event(self, kind: str, **data) -> dict:
        entry = {"t": round(time.time() - self._t0, 3), "type": kind, **data}
        with self._log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def next_shot_path(self, label: str) -> Path:
        self._shot_index += 1
        safe = re.sub(r"[^a-z0-9_-]+", "_", label.lower())[:40] or "shot"
        return self.shots / f"{self._shot_index:02d}-{safe}.png"

    def record_action(self, description: str) -> None:
        self.actions.append(description)

    def add_finding(self, finding: Finding) -> Finding:
        if not finding.repro:
            finding.repro = list(self.actions[-8:])
        self.findings.append(finding)
        self.event("finding", **finding.as_dict())
        return finding

    # -- outputs -----------------------------------------------------------
    def write_run_json(self, **fields) -> Path:
        path = self.dir / "run.json"
        payload = {
            "startedAt": datetime.fromtimestamp(self._t0, tz=timezone.utc).isoformat(),
            "durationSeconds": round(time.time() - self._t0, 1),
            "findings": {sev: sum(1 for f in self.findings if f.severity == sev) for sev in SEVERITY_ORDER},
            **fields,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def write_findings_md(self, *, title: str, meta_lines: list[str], summary: str = "") -> Path:
        path = self.dir / "findings.md"
        lines = [f"# {title}", ""]
        lines += meta_lines
        lines.append("")
        if summary:
            lines += ["## Tester summary", "", summary.strip(), ""]
        lines.append("## Findings")
        if not self.findings:
            lines += ["", "- none recorded"]
        for sev in SEVERITY_ORDER:
            group = [f for f in self.findings if f.severity == sev]
            if not group:
                continue
            lines += ["", f"### {sev.capitalize()}", ""]
            for i, f in enumerate(group, start=1):
                tag = f"[{sev[0].upper()}-{i}]"
                status = f" _{f.status}_" if f.status else ""
                lines.append(f"**{tag} {f.title}**{status} (step {f.step}, {f.source})")
                lines.append("")
                lines.append(f.detail.strip() or "(no detail)")
                if f.url:
                    lines.append(f"URL: {f.url}")
                if f.screenshot:
                    lines.append(f"Screenshot: `{f.screenshot}`")
                if f.repro:
                    lines.append("Repro:")
                    lines += [f"  {n}. {a}" for n, a in enumerate(f.repro, start=1)]
                lines.append("")
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path


def load_run(run_dir: Path) -> dict:
    """Read run.json, findings (from log.jsonl) and the ordered step actions."""
    run_dir = Path(run_dir)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8")) if (run_dir / "run.json").exists() else {}
    findings: list[Finding] = []
    steps: list[dict] = []
    log = run_dir / "log.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("type") == "finding":
                findings.append(Finding(**{k: v for k, v in entry.items() if k in Finding.__dataclass_fields__}))
            elif entry.get("type") == "step":
                steps.append(entry)
    return {"dir": str(run_dir), "run": run, "findings": findings, "steps": steps}
