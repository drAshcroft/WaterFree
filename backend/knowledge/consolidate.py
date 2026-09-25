"""Consolidation pass over the global knowledge store ("dreaming").

An append-only store fills up with near-duplicates, relative dates that stop
meaning anything, and entries nobody ever retrieves. This module reports those
three things and, on request, applies the safe fixes:

- **duplicate groups**: entries whose title/description vocabulary overlaps
  strongly; a local model proposes one merged entry per group, applied with
  `update_entry` on the oldest id (so citations keep resolving) and `delete`
  on the rest;
- **relative dates**: "yesterday", "last week", "2 days ago" ... rewritten to
  absolute dates anchored on the entry's creation timestamp (deterministic, no
  model);
- **never retrieved**: entries with no retrieval in the usage log for the
  window, listed for a human to prune (never deleted automatically).

Report first; `apply=True` performs the merges and date rewrites only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from typing import Callable, Iterable

from backend.cli import usage_log
from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.store import DuplicateContentError, KnowledgeStore

ChatFn = Callable[[str, str], str]  # (system, user) -> assistant text

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset("""a an and are as at be by for from has have in is it its of on or that the this to was
were will with when which who whom why how not no yes into than then them they their there these those
your you we our us can could should would may might must do does did done use used using""".split())

_RELATIVE_DATE_RE = re.compile(
    r"\b(yesterday|today|tonight|last (?:night|week|month|year)|this (?:week|month|year)|"
    r"(?:a|an|one|two|three|four|five|six|seven|\d+) (?:day|week|month|year)s? ago|recently|earlier this (?:week|month|year))\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


@dataclass
class DuplicateGroup:
    ids: list[str]
    titles: list[str]
    similarity: float
    hierarchy_paths: list[str]
    proposal: dict | None = None      # {"title", "description", "context", "tags"} from the model
    applied: bool = False
    error: str = ""


@dataclass
class DateRewrite:
    id: str
    title: str
    field_name: str
    before: str
    after: str
    applied: bool = False


@dataclass
class ConsolidationReport:
    total_entries: int
    window_days: int
    retrieval_records: int
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    date_rewrites: list[DateRewrite] = field(default_factory=list)
    never_retrieved: list[dict] = field(default_factory=list)
    no_hierarchy: list[dict] = field(default_factory=list)
    applied: bool = False

    def to_dict(self) -> dict:
        return {
            "total_entries": self.total_entries,
            "window_days": self.window_days,
            "retrieval_records": self.retrieval_records,
            "applied": self.applied,
            "summary": {
                "duplicate_groups": len(self.duplicate_groups),
                "mergeable_groups": sum(1 for g in self.duplicate_groups if not g.error),
                "duplicate_entries": sum(len(g.ids) for g in self.duplicate_groups),
                "date_rewrites": len(self.date_rewrites),
                "never_retrieved": len(self.never_retrieved),
                "no_hierarchy": len(self.no_hierarchy),
            },
            "duplicate_groups": [g.__dict__ for g in self.duplicate_groups],
            "date_rewrites": [d.__dict__ for d in self.date_rewrites],
            "never_retrieved": self.never_retrieved,
            "no_hierarchy": self.no_hierarchy,
        }


# ── vocabulary similarity ──────────────────────────────────────────────────

def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_RE.findall(text.casefold()) if len(t) > 2 and t not in _STOP)


def _vocab(entry: KnowledgeEntry) -> frozenset[str]:
    return _tokens(f"{entry.title} {entry.description}")


def _title_vocab(entry: KnowledgeEntry) -> frozenset[str]:
    return _tokens(re.sub(r"^(?:RESOLVED|COMPLAINT|ISSUE|FIX|TODO)\W+", "", entry.title, flags=re.IGNORECASE))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_duplicate_groups(entries: list[KnowledgeEntry], *, threshold: float = 0.5) -> list[DuplicateGroup]:
    """Union-find over pairs that look like the same lesson filed twice.

    Two signals, either is enough: the titles' vocabulary overlaps ≥ threshold
    (titles are short, so this is a strong match and hierarchy is ignored: the
    same lesson is often filed under two paths), or the title+description
    vocabulary overlaps ≥ threshold *and* the entries share a top-level
    hierarchy segment (a Godot camera note and a Phaser camera note are not
    duplicates even when their words overlap).
    """
    vocab = {e.id: _vocab(e) for e in entries}
    titles = {e.id: _title_vocab(e) for e in entries}
    by_id = {e.id: e for e in entries}
    parent = {e.id: e.id for e in entries}
    best: dict[tuple[str, str], float] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def top(e: KnowledgeEntry) -> str:
        segments = e.effective_hierarchy_segments()
        return segments[0] if segments else ""

    for a, b in combinations(entries, 2):
        score = _jaccard(titles[a.id], titles[b.id])
        if score < threshold and top(a) == top(b):
            score = _jaccard(vocab[a.id], vocab[b.id])
        if score >= threshold:
            parent[find(a.id)] = find(b.id)
            best[(a.id, b.id)] = score

    groups: dict[str, list[str]] = {}
    for e in entries:
        groups.setdefault(find(e.id), []).append(e.id)

    out: list[DuplicateGroup] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: by_id[i].created_at)   # oldest first: it survives
        scores = [s for (x, y), s in best.items() if x in members and y in members]
        out.append(DuplicateGroup(
            ids=members,
            titles=[by_id[i].title for i in members],
            similarity=round(max(scores) if scores else threshold, 2),
            hierarchy_paths=[by_id[i].effective_hierarchy_path() for i in members],
        ))
    out.sort(key=lambda g: (-len(g.ids), -g.similarity))
    return out


# ── relative dates ─────────────────────────────────────────────────────────

def _anchor(entry: KnowledgeEntry) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(entry.created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _absolute(phrase: str, anchor: datetime) -> str | None:
    text = phrase.casefold()
    day = anchor.date()
    if text in ("today", "tonight"):
        return day.isoformat()
    if text == "yesterday":
        return (day - timedelta(days=1)).isoformat()
    if text == "last night":
        return (day - timedelta(days=1)).isoformat()
    if text == "last week":
        return f"the week of {(day - timedelta(days=7)).isoformat()}"
    if text == "last month":
        return (day.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if text == "last year":
        return str(day.year - 1)
    if text.startswith("this ") or text.startswith("earlier this "):
        unit = text.rsplit(" ", 1)[-1]
        return {"week": f"the week of {day.isoformat()}", "month": day.strftime("%Y-%m"), "year": str(day.year)}[unit]
    if text == "recently":
        return f"around {day.isoformat()}"
    match = re.match(r"(a|an|one|two|three|four|five|six|seven|\d+) (day|week|month|year)s? ago", text)
    if match:
        amount = _NUMBER_WORDS.get(match.group(1)) or int(match.group(1))
        unit = match.group(2)
        if unit == "day":
            return (day - timedelta(days=amount)).isoformat()
        if unit == "week":
            return (day - timedelta(weeks=amount)).isoformat()
        if unit == "month":
            month = (day.month - amount - 1) % 12 + 1
            year = day.year + (day.month - amount - 1) // 12
            return f"{year:04d}-{month:02d}"
        if unit == "year":
            return str(day.year - amount)
    return None


def rewrite_relative_dates(text: str, anchor: datetime) -> str:
    """Replace relative phrases with absolute ones, e.g. 'yesterday' -> 'on 2026-09-24'."""
    def sub(match: re.Match) -> str:
        phrase = match.group(0)
        absolute = _absolute(phrase, anchor)
        if absolute is None:
            return phrase
        return f"{phrase} ({absolute})"
    return _RELATIVE_DATE_RE.sub(sub, text)


def find_date_rewrites(entries: Iterable[KnowledgeEntry]) -> list[DateRewrite]:
    out: list[DateRewrite] = []
    for entry in entries:
        anchor = _anchor(entry)
        if anchor is None:
            continue
        for field_name in ("description", "context"):
            before = getattr(entry, field_name) or ""
            if not before or not _RELATIVE_DATE_RE.search(before):
                continue
            # Skip phrases already followed by an absolute date in parentheses.
            if re.search(r"\((?:around |the week of )?\d{4}(?:-\d{2}){0,2}\)", before):
                continue
            after = rewrite_relative_dates(before, anchor)
            if after != before:
                out.append(DateRewrite(id=entry.id, title=entry.title, field_name=field_name, before=before, after=after))
    return out


# ── retrieval statistics ───────────────────────────────────────────────────

def retrieval_counts(records: Iterable[dict], *, since: datetime | None) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = {}
    considered = 0
    for record in records:
        if record.get("area") != "knowledge" or record.get("action") not in ("search", "browse", "get", "inject"):
            continue
        if since is not None:
            try:
                stamp = datetime.fromisoformat(str(record.get("ts", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp < since:
                continue
        considered += 1
        for entry_id in record.get("hit_ids") or []:
            counts[entry_id] = counts.get(entry_id, 0) + 1
        if record.get("action") == "get":
            for arg in record.get("argv") or []:
                if isinstance(arg, str) and len(arg) == 36:
                    counts[arg] = counts.get(arg, 0) + 1
    return counts, considered


# ── model-backed merge proposals ───────────────────────────────────────────

_MERGE_SYSTEM = (
    "You merge near-duplicate knowledge-base entries written for AI coding agents. "
    "Return ONLY a JSON object with keys title, description, context, tags. "
    "Write the description as an instruction (what to do, when, why), not a story; keep every "
    "concrete fact, number and file name from the inputs; drop repetition; keep dates absolute; "
    "tags is a list of 3-6 short lowercase strings."
)


def propose_merge(group: DuplicateGroup, entries: dict[str, KnowledgeEntry], chat: ChatFn) -> dict:
    parts = []
    for entry_id in group.ids:
        e = entries[entry_id]
        parts.append(
            f"### Entry {entry_id} (created {e.created_at[:10]}, type {e.snippet_type})\n"
            f"Title: {e.title}\nDescription: {e.description}\nContext: {e.context}\n"
            f"Tags: {', '.join(e.tags)}\nCode ({len(e.code)} chars): {e.code[:600]}"
        )
    raw = chat(_MERGE_SYSTEM, "Merge these entries into one:\n\n" + "\n\n".join(parts))
    text = raw.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("model did not return a JSON object")
    data = json.loads(match.group(0))
    proposal = {
        "title": str(data.get("title") or entries[group.ids[0]].title).strip(),
        "description": str(data.get("description") or "").strip(),
        "context": str(data.get("context") or "").strip(),
        "tags": [str(t).strip().casefold() for t in (data.get("tags") or []) if str(t).strip()][:6],
    }
    if not proposal["description"]:
        raise ValueError("merged description is empty")
    return proposal


# ── orchestration ──────────────────────────────────────────────────────────

def consolidate(
    store: KnowledgeStore,
    *,
    window_days: int = 90,
    min_age_days: int = 30,
    duplicate_threshold: float = 0.5,
    chat: ChatFn | None = None,
    apply: bool = False,
    max_groups: int = 25,
    max_group_size: int = 5,
    include_assets: bool = False,
    usage_paths: list[Path] | None = None,
    now: datetime | None = None,
) -> ConsolidationReport:
    """Build the report; see the module docstring.

    `window_days` bounds the retrieval history that counts; `min_age_days`
    keeps entries younger than that out of the never-retrieved list (nobody has
    had a chance to look them up). Asset-catalog rows (hierarchy `assets/...`)
    are templated and skipped unless `include_assets`; a duplicate group larger
    than `max_group_size` is reported as a cluster but never sent to the model
    or merged, because that many "duplicates" is a template, not a repeat.
    """
    now = now or datetime.now(timezone.utc)
    entries = store._all_entries()
    if not include_assets:
        entries = [e for e in entries if (e.effective_hierarchy_segments() or [""])[0] != "assets"]
    by_id = {e.id: e for e in entries}

    paths = usage_paths if usage_paths is not None else _default_usage_paths()
    counts, considered = retrieval_counts(usage_log.read_records(paths), since=now - timedelta(days=window_days))

    report = ConsolidationReport(total_entries=len(entries), window_days=window_days, retrieval_records=considered)

    report.duplicate_groups = find_duplicate_groups(entries, threshold=duplicate_threshold)[:max_groups]
    for group in report.duplicate_groups:
        if len(group.ids) > max_group_size:
            group.error = f"{len(group.ids)} entries share one template; too many to merge, split them by hand"
    if chat is not None:
        for group in report.duplicate_groups:
            if group.error:
                continue
            try:
                group.proposal = propose_merge(group, by_id, chat)
            except Exception as exc:  # model hiccup: report it, keep going
                group.error = f"proposal failed: {exc}"

    report.date_rewrites = find_date_rewrites(entries)

    cutoff = now - timedelta(days=min_age_days)
    for e in entries:
        created = _anchor(e)
        if e.id in counts:
            continue
        if created is not None and created > cutoff:
            continue  # too young to judge
        report.never_retrieved.append({
            "id": e.id, "title": e.title, "created_at": e.created_at[:10],
            "source_repo": e.source_repo, "hierarchy_path": e.effective_hierarchy_path(),
        })
    report.never_retrieved.sort(key=lambda r: r["created_at"])

    report.no_hierarchy = [
        {"id": e.id, "title": e.title, "source_repo": e.source_repo}
        for e in entries if not e.hierarchy_path
    ]

    if apply:
        _apply(store, report, by_id)
        report.applied = True
    return report


def _apply(store: KnowledgeStore, report: ConsolidationReport, by_id: dict[str, KnowledgeEntry]) -> None:
    merged_ids: set[str] = set()
    for group in report.duplicate_groups:
        if not group.proposal or group.error:
            continue
        survivor, *rest = group.ids
        try:
            store.update_entry(
                survivor,
                title=group.proposal["title"],
                description=group.proposal["description"],
                context=group.proposal["context"] or by_id[survivor].context,
                tags=group.proposal["tags"] or by_id[survivor].tags,
            )
        except (KeyError, DuplicateContentError, ValueError) as exc:
            group.error = f"apply failed: {exc}"
            continue
        for entry_id in rest:
            store.delete_entry(entry_id)
            merged_ids.add(entry_id)
        merged_ids.add(survivor)   # its text was just rewritten by the model; the old rewrite no longer applies
        group.applied = True

    for rewrite in report.date_rewrites:
        if rewrite.id in merged_ids:
            continue
        try:
            store.update_entry(rewrite.id, **{rewrite.field_name: rewrite.after})
        except (KeyError, DuplicateContentError, ValueError):
            continue
        rewrite.applied = True


def _default_usage_paths() -> list[Path]:
    paths = [usage_log.transcript_log_path()]
    live = usage_log.log_path()
    if live is not None:
        paths.insert(0, live)
    return paths


def ollama_chat_fn(*, workspace_path: str = "", model: str = "") -> ChatFn:
    """A ChatFn on the workspace's `knowledge_consolidate` stage, falling back to local Ollama."""
    from backend.llm.chat_client import chat as _chat, preflight, resolve_chat_target

    target = resolve_chat_target(
        stage="knowledge_consolidate",
        workspace_path=workspace_path,
        fallback_model=model or "freehuntx/qwen3-coder:14b",
    )
    preflight(target)

    def run(system: str, user: str) -> str:
        return _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            target=target,
            max_tokens=1200,
        )

    return run
