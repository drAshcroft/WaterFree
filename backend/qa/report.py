"""Consolidate several run directories into one markdown report.

A sweep is several personas against the same build, so the interesting question
is not "what did the impatient one find" but "what did more than one of them
trip over". Findings are therefore grouped by similarity across runs, and a
finding seen by two personas is ranked above one seen by a single persona at
the same severity -- independent rediscovery is the cheapest corroboration
available.

Verified status, when a run has been through `qa verify`, is carried through:
a confirmed finding sorts above an unreproduced one.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from backend.qa.findings import SEVERITY_ORDER, Finding, load_run

_SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITY_ORDER)}
_STATUS_RANK = {"confirmed": 0, "": 1, "unreproduced": 2, "error": 3}

# Words that carry no signal when deciding whether two findings are the same.
_STOPWORDS = frozenset(
    "the a an is are was were and or but not no it its this that then than "
    "to of in on at by for with from as i you when there here be been".split()
)


@dataclass
class Group:
    """One finding, plus the other runs that reported something like it."""

    finding: Finding
    runs: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)

    @property
    def best_status(self) -> str:
        if not self.statuses:
            return ""
        return sorted(self.statuses, key=lambda s: _STATUS_RANK.get(s, 1))[0]

    @property
    def sort_key(self) -> tuple:
        return (
            _SEVERITY_RANK.get(self.finding.severity, 9),
            _STATUS_RANK.get(self.best_status, 1),
            -len(self.runs),
            self.finding.title.lower(),
        )


def _fingerprint(finding: Finding) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", finding.title.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 2)


def _same(a: frozenset[str], b: frozenset[str], *, threshold: float = 0.6) -> bool:
    if not a or not b:
        return a == b
    return len(a & b) / len(a | b) >= threshold


def collect(run_dirs: list[Path]) -> tuple[list[Group], list[dict]]:
    """Group findings across runs. Returns (groups, per-run metadata)."""
    groups: list[Group] = []
    fingerprints: list[frozenset[str]] = []
    meta: list[dict] = []

    for run_dir in run_dirs:
        loaded = load_run(Path(run_dir))
        run = loaded["run"]
        label = (run.get("persona") or {}).get("name") or Path(run_dir).name
        outcome = run.get("outcome") or {}
        meta.append({
            "dir": str(run_dir),
            "persona": label,
            "url": run.get("url", ""),
            "goal": run.get("goal", ""),
            "model": run.get("model", ""),
            "reason": outcome.get("reason", ""),
            "steps": outcome.get("steps", 0),
            "findings": len(loaded["findings"]),
        })

        for finding in loaded["findings"]:
            print_ = _fingerprint(finding)
            for index, existing in enumerate(fingerprints):
                if groups[index].finding.severity == finding.severity and _same(existing, print_):
                    if label not in groups[index].runs:
                        groups[index].runs.append(label)
                    groups[index].statuses.append(finding.status)
                    break
            else:
                groups.append(Group(finding=finding, runs=[label], statuses=[finding.status]))
                fingerprints.append(print_)

    groups.sort(key=lambda g: g.sort_key)
    return groups, meta


def render(run_dirs: list[Path], *, title: str = "QA sweep") -> str:
    groups, meta = collect(run_dirs)
    lines = [f"# {title}", ""]

    lines.append("## Runs")
    lines.append("")
    lines.append("| Persona | Goal | Ended | Steps | Findings |")
    lines.append("|---|---|---|---|---|")
    for item in meta:
        lines.append(
            f"| {item['persona']} | {str(item['goal'])[:48]} | {item['reason']} "
            f"| {item['steps']} | {item['findings']} |"
        )
    if meta:
        lines += ["", f"URL under test: {meta[0]['url']}", f"Driver model: {meta[0]['model']}"]

    lines += ["", "## Findings", ""]
    if not groups:
        lines.append("No findings were recorded across these runs.")
        return "\n".join(lines) + "\n"

    corroborated = [g for g in groups if len(g.runs) > 1]
    if corroborated:
        lines += [
            f"{len(corroborated)} finding(s) were reported by more than one persona; "
            "those are listed first within their severity.",
            "",
        ]

    for severity in SEVERITY_ORDER:
        section = [g for g in groups if g.finding.severity == severity]
        if not section:
            continue
        lines += [f"### {severity.capitalize()}", ""]
        for index, group in enumerate(section, start=1):
            finding = group.finding
            tag = f"[{severity[0].upper()}-{index}]"
            bits = [f"**{tag} {finding.title}**"]
            if group.best_status:
                bits.append(f"_{group.best_status}_")
            bits.append(f"({finding.source}, seen by: {', '.join(group.runs)})")
            lines.append(" ".join(bits))
            lines.append("")
            if finding.detail.strip():
                lines += [finding.detail.strip(), ""]
            if finding.url:
                lines.append(f"URL: {finding.url}")
            if finding.repro:
                lines.append("Repro:")
                lines += [f"  {n}. {step}" for n, step in enumerate(finding.repro, start=1)]
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


MAX_SEARCH_DEPTH = 4


def find_run_dirs(paths: list[str]) -> list[Path]:
    """Expand each path to the run directories under it.

    A path may be one run directory (holding run.json), a sweep directory
    holding one per persona, or the runs root holding several sweeps -- which
    is what a caller naturally passes, since it is the `--out` they gave. A
    single-level glob only matched the middle case and reported "no run
    directories" for the other one.

    Depth-limited rather than a bare rglob: pointed at a home directory by
    mistake, this should fail fast instead of walking the disk.
    """
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if (path / "run.json").is_file():
            found.append(path)
            continue
        if path.is_dir():
            for depth in range(1, MAX_SEARCH_DEPTH + 1):
                pattern = "/".join(["*"] * depth) + "/run.json"
                found += sorted(p.parent for p in path.glob(pattern))
    # Deduplicate, preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def counts_by_severity(groups: list[Group]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for group in groups:
        out[group.finding.severity] += 1
    return dict(out)
