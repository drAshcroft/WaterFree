"""`waterfree knowledge ...` — global knowledge store CLI."""

from __future__ import annotations

import difflib
import json
import re
import sys
from argparse import Namespace, _SubParsersAction
from datetime import datetime, timezone
from pathlib import Path

from backend.cli._common import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    add_full_arg,
    add_workspace_arg,
    emit_error,
    emit_json,
    emit_raw,
)
from backend.knowledge.models import SCOPES, KnowledgeEntry, normalize_hierarchy_path
from backend.knowledge.source_ref import ReaderContext, capture_source_ref
from backend.knowledge.store import (
    SEARCH_SCOPES,
    AmbiguousEntryError,
    DuplicateContentError,
    KnowledgeStore,
    _repo_key,
)

_SNIPPET_TYPES = ("pattern", "utility", "style", "api_usage", "convention")
_HISTORY_FIELDS = ("title", "description", "code", "snippet_type", "tags", "context", "source_repo",
                   "source_file", "source_repo_url", "hierarchy_path", "scope", "source_ref")


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("knowledge", help="Global knowledge / snippet store")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_search = actions.add_parser(
        "search",
        help="Full-text search. Entries matching every word rank first; the "
             "current workspace's own entries come before other projects'.",
    )
    add_workspace_arg(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--path", default="", help="Limit results to this hierarchy path and its descendants (case-insensitive).")
    p_search.add_argument("--repo", default="",
                          help="Only entries from this source repo (name or path).")
    p_search.add_argument(
        "--scope", default="default", choices=SEARCH_SCOPES,
        help="default = global entries plus the current workspace's own project entries; "
             "all; or exactly global / project / assets.",
    )
    search_shape = p_search.add_mutually_exclusive_group()
    search_shape.add_argument(
        "--full", action="store_true",
        help="Include each entry's code and context. Default rows carry the id, "
             "title, description, type, repo, tags and sizes only; use `get <id>` "
             "for one entry's body.",
    )
    search_shape.add_argument(
        "--brief", action="store_true",
        help="Titles only: each row is id, title, hierarchy_path and source_repo, one "
             "minified line per row (about a quarter of a default row). Use for duplicate "
             "checks and orientation.",
    )

    p_browse = actions.add_parser(
        "browse",
        help="Walk the knowledge taxonomy. A path that matches no node returns a `hint` "
             "with the nearest existing paths.",
    )
    add_workspace_arg(p_browse)
    p_browse.add_argument("path_arg", nargs="?", default=None, metavar="PATH",
                          help="Taxonomy path, e.g. procedural/buildings (same as --path).")
    p_browse.add_argument("--path", default="",
                          help="Taxonomy path; case, spaces and '\\' separators are normalised.")
    p_browse.add_argument("--depth", type=int, default=2)
    p_browse.add_argument("--include-entries", action="store_true",
                          help="Include the branch's entries as compact rows (search-row shape; "
                               "--full adds code and context).")
    p_browse.add_argument("--list", action="store_true", help="Same as --include-entries.")
    p_browse.add_argument("--entry-limit", type=int, default=10)
    add_full_arg(p_browse)

    p_get = actions.add_parser(
        "get", help="Fetch one knowledge entry by id, unique id prefix (6+ chars) or exact title",
    )
    add_workspace_arg(p_get)
    p_get.add_argument("entry_id")
    p_get.add_argument("--text", action="store_true", help="Print readable fields and the code body with actual newlines.")

    p_history = actions.add_parser(
        "history",
        help="Prior values of an entry's fields, newest first: one row per field an update "
             "replaced, plus a full snapshot when the entry was deleted.",
    )
    add_workspace_arg(p_history)
    p_history.add_argument("entry_id", help="Entry id, unique id prefix or exact title (a deleted entry: id or prefix).")
    p_history.add_argument("--field", action="append", default=None, choices=_HISTORY_FIELDS,
                           help="Only this field (repeatable).")
    p_history.add_argument("--limit", type=int, default=50)
    add_full_arg(p_history)

    p_add = actions.add_parser("add", help="Add a knowledge entry")
    add_workspace_arg(p_add)
    p_add.add_argument("--title", required=True)
    add_description = p_add.add_mutually_exclusive_group(required=True)
    add_description.add_argument("--description")
    add_description.add_argument("--description-file", help="Read a UTF-8 description file, or '-' for stdin.")
    p_add.add_argument("--dry-run", action="store_true", help="Validate and preview an entry without saving it.")
    code_group = p_add.add_mutually_exclusive_group()
    code_group.add_argument("--code", help="Inline code body (small snippets only)")
    code_group.add_argument("--code-file",
                            help="Read code body from a file. Use '-' for stdin.")
    p_add.add_argument(
        "--snippet-type", default=None, choices=_SNIPPET_TYPES,
        help="Required when code is given; defaults to 'convention' for a prose-only entry.",
    )
    p_add.add_argument("--source-repo", required=True)
    p_add.add_argument("--source-file", default="",
                       help="Path of the file the lesson is about, relative to --workspace (or cwd). Inside "
                            "a git checkout its branch, worktree and committed/uncommitted status are recorded.")
    p_add.add_argument("--tag", action="append", default=[],
                       help="Repeatable. e.g. --tag python --tag retry")
    add_context = p_add.add_mutually_exclusive_group()
    add_context.add_argument("--context", default="")
    add_context.add_argument("--context-file", default=None, help="Read a UTF-8 context file, or '-' for stdin.")
    p_add.add_argument("--quiet", action="store_true",
                       help="Print only {id, added, revision}; warnings still go to stderr.")
    p_add.add_argument("--source-repo-url", default="")
    p_add.add_argument("--hierarchy-path", default="",
                       help="Slash-separated taxonomy path, e.g. platform/auth/jwt")
    p_add.add_argument(
        "--no-check", action="store_true",
        help="Skip the quality gate (near-duplicate title, missing hierarchy path, trace-like description).",
    )
    p_add.add_argument("--force", action="store_true", help="Allow a near-duplicate title after reviewing its warning.")
    p_add.add_argument(
        "--scope", default=None, choices=SCOPES,
        help="global (default: any project may see it), project (only searches from its own "
             "source repo), assets (catalog row; the default for assets/* paths).",
    )

    p_update = actions.add_parser(
        "update",
        help="Revise an entry in place. The id is kept and 'revision' is bumped; "
             "only the fields you pass change, and the replaced values are kept "
             "(see `history`).",
    )
    add_workspace_arg(p_update)
    p_update.add_argument("entry_id", help="Entry id, unique id prefix (6+ chars) or exact title.")
    p_update.add_argument("--title", default=None)
    update_description = p_update.add_mutually_exclusive_group()
    update_description.add_argument("--description", default=None, help="Replace the description.")
    update_description.add_argument("--description-file", help="Replace the description from a UTF-8 file, or '-' for stdin.")
    update_description.add_argument("--append-description", default=None, metavar="TEXT",
                                    help="Add TEXT as a new paragraph after the current description.")
    update_code = p_update.add_mutually_exclusive_group()
    update_code.add_argument("--code", default=None, help="Replace the code body inline.")
    update_code.add_argument("--code-file", default=None,
                             help="Replace the code body from a file. Use '-' for stdin.")
    p_update.add_argument("--snippet-type", default=None, choices=_SNIPPET_TYPES)
    p_update.add_argument("--tag", action="append", default=None,
                          help="Repeatable. Replaces the whole tag list when given.")
    update_context = p_update.add_mutually_exclusive_group()
    update_context.add_argument("--context", default=None, help="Replace the context.")
    update_context.add_argument("--context-file", default=None,
                                help="Replace the context from a UTF-8 file, or '-' for stdin.")
    update_context.add_argument("--append-context", default=None, metavar="TEXT",
                                help="Add TEXT as a new paragraph after the current context.")
    p_update.add_argument("--source-repo", default=None)
    p_update.add_argument("--source-file", default=None,
                          help="Replace the source file; its git branch/worktree/commit status is re-recorded.")
    p_update.add_argument("--source-repo-url", default=None)
    p_update.add_argument("--hierarchy-path", default=None)
    p_update.add_argument("--scope", default=None, choices=SCOPES)
    p_update.add_argument("--restore-revision", type=int, default=None, metavar="N",
                          help="Put every field back to its value at revision N (see `history`). "
                               "Cannot be combined with field flags.")
    p_update.add_argument("--restore-field", action="append", default=None, choices=_HISTORY_FIELDS,
                          help="With --restore-revision: restore only this field (repeatable).")
    p_update.add_argument("--quiet", action="store_true",
                          help="Print only {id, updated, revision}; warnings still go to stderr.")

    p_hook = actions.add_parser(
        "hook-context",
        help="Claude Code UserPromptSubmit hook: read the hook JSON on stdin, search the "
             "prompt (all terms must match), and print additionalContext with at most a "
             "few summary rows. Prints nothing when no entry matches every term.",
    )
    add_workspace_arg(p_hook)
    p_hook.add_argument("--max-rows", type=int, default=3)
    p_hook.add_argument("--min-terms", type=int, default=2,
                        help="Prompts with fewer distinct search terms are ignored (default 2).")
    p_hook.add_argument("--max-terms", type=int, default=12,
                        help="Only the first N distinct terms of a long prompt are searched (default 12).")

    p_hook_session = actions.add_parser(
        "hook-session",
        help="Claude Code / Codex SessionStart hook: one short orientation line about the "
             "knowledge store for this project (counts, most-retrieved entries). Reads the "
             "event JSON on stdin; prints nothing for an empty store.",
    )
    add_workspace_arg(p_hook_session)
    p_hook_session.add_argument("--max-rows", type=int, default=3)

    p_hook_stop = actions.add_parser(
        "hook-stop",
        help="Claude Code / Codex Stop hook: when the final message reads like a hard-won "
             "lesson (several attempts, a workaround, a root cause), ask once for it to be "
             "filed with `knowledge add`. Reads the event JSON on stdin.",
    )
    add_workspace_arg(p_hook_stop)

    p_hook_pretool = actions.add_parser(
        "hook-pretool",
        help="Claude Code / Codex PreToolUse hook (matcher Bash): when the command sends an "
             "edit through a heredoc whose body holds backslashes, add a warning that they "
             "will be mangled. Never blocks; always exits 0. backend/main.py runs it on a "
             "fast path that skips this dispatcher.",
    )
    add_workspace_arg(p_hook_pretool)

    p_consolidate = actions.add_parser(
        "consolidate",
        help="Consolidation pass: near-duplicate groups (merged by a local model), "
             "relative dates made absolute, entries never retrieved, entries without "
             "a taxonomy path. Reports by default; --apply performs merges and date rewrites.",
    )
    add_workspace_arg(p_consolidate)
    p_consolidate.add_argument("--window", type=int, default=90,
                               help="Days of usage-log history that count as 'retrieved' (default 90).")
    p_consolidate.add_argument("--min-age", type=int, default=30,
                               help="Entries younger than this many days are never listed as stale (default 30).")
    p_consolidate.add_argument("--max-group-size", type=int, default=5,
                               help="Duplicate groups larger than this are reported but not merged (default 5).")
    p_consolidate.add_argument("--include-assets", action="store_true",
                               help="Also consider assets/* catalog rows (skipped by default).")
    p_consolidate.add_argument("--threshold", type=float, default=0.5,
                               help="Vocabulary overlap (Jaccard) of titles, or of title+description within one "
                                    "hierarchy, that makes two entries duplicates (default 0.5).")
    p_consolidate.add_argument("--max-groups", type=int, default=25,
                               help="Largest duplicate groups to send to the model (default 25).")
    p_consolidate.add_argument("--no-llm", action="store_true",
                               help="Skip merge proposals; still reports groups, dates and stale entries.")
    p_consolidate.add_argument("--model", default="",
                               help="Ollama model for merge proposals (default: the knowledge_consolidate stage, else freehuntx/qwen3-coder:14b).")
    p_consolidate.add_argument("--apply", action="store_true",
                               help="Apply merges (oldest id survives, others deleted) and date rewrites. Never deletes stale entries.")
    p_consolidate.add_argument("--report-file", default="",
                               help="Also write the full report JSON to this path.")

    p_delete = actions.add_parser(
        "delete",
        help="Remove a knowledge entry by id or unique id prefix (6+ chars). Its fields stay "
             "readable with `history <id>`.",
    )
    add_workspace_arg(p_delete)
    p_delete.add_argument("entry_id")

    p_list_sources = actions.add_parser("list-sources", help="List all indexed repos/sources")
    add_workspace_arg(p_list_sources)
    add_full_arg(p_list_sources)

    p_stats = actions.add_parser("stats", help="Summary statistics")
    add_workspace_arg(p_stats)
    p_stats.add_argument("--depth", type=int, default=1, help="Include hierarchy categories to this depth.")
    add_full_arg(p_stats)

    p.set_defaults(_runner=run)


def _entry_to_dict(entry: KnowledgeEntry) -> dict:
    return {
        "id": entry.id,
        "title": entry.title,
        "description": entry.description,
        "snippet_type": entry.snippet_type,
        "code": entry.code,
        "tags": entry.tags,
        "context": entry.context,
        "source_repo": entry.source_repo,
        "source_file": entry.source_file,
        "source_repo_url": entry.source_repo_url,
        "created_at": entry.created_at,
        "hierarchy_path": entry.effective_hierarchy_path(),
        "hierarchy_segments": entry.effective_hierarchy_segments(),
        "hierarchy_source": entry.hierarchy_source(),
        "updated_at": entry.updated_at,
        "revision": entry.revision,
        "scope": entry.scope,
        "source_ref": entry.source_ref,
    }


_SUMMARY_DESCRIPTION_CHARS = 320


def _entry_brief(entry: KnowledgeEntry) -> dict:
    """The titles-only row (`search --brief`): enough to recognise an entry and fetch it."""
    return {
        "id": entry.id,
        "title": entry.title,
        "hierarchy_path": entry.effective_hierarchy_path(),
        "source_repo": entry.source_repo,
    }


class _RowShaper:
    """Shapes entry rows for one command, adding the reader-relative source note.

    The note says when the entry's source file was uncommitted in another
    worktree when filed, or is missing from the reader's checkout of the same
    repo. Built once per command: one filesystem walk for the reader, one
    existence check per same-repo row, no git calls.
    """

    def __init__(self, workspace: str | None):
        self._reader = ReaderContext.for_workspace(workspace)

    def note(self, entry: KnowledgeEntry) -> str | None:
        try:
            return self._reader.note_for(source_ref=entry.source_ref, source_file=entry.source_file,
                                         source_repo=entry.source_repo)
        except OSError:  # pragma: no cover - unreadable workspace
            return None

    def _with_note(self, row: dict, entry: KnowledgeEntry) -> dict:
        note = self.note(entry)
        if note:
            row["source_note"] = note
        return row

    def summary(self, entry: KnowledgeEntry) -> dict:
        return self._with_note(_entry_summary(entry), entry)

    def full(self, entry: KnowledgeEntry) -> dict:
        return self._with_note(_entry_to_dict(entry), entry)

    @staticmethod
    def brief(entry: KnowledgeEntry) -> dict:
        return _entry_brief(entry)


def _entry_summary(entry: KnowledgeEntry) -> dict:
    """The compact search row: enough to choose an entry, never its body."""
    description = " ".join(entry.description.split())
    if len(description) > _SUMMARY_DESCRIPTION_CHARS:
        description = description[:_SUMMARY_DESCRIPTION_CHARS].rstrip() + "…"
    return {
        "id": entry.id,
        "title": entry.title,
        "description": description,
        "snippet_type": entry.snippet_type,
        "source_repo": entry.source_repo,
        "source_file": entry.source_file,
        "tags": entry.tags,
        "hierarchy_path": entry.effective_hierarchy_path(),
        "code_chars": len(entry.code),
        "context_chars": len(entry.context),
        "revision": entry.revision,
        "scope": entry.scope,
    }


def _repo_to_dict(repo) -> dict:
    return {
        "name": repo.name,
        "local_path": repo.local_path,
        "remote_url": repo.remote_url,
        "entry_count": repo.entry_count,
        "last_indexed": repo.last_indexed,
    }


_HOOK_STOP = frozenset("""a an and are as at be by for from has have in is it its of on or that the this to was
were will with when which who whom why how not no yes into than then them they their there these those
your you we our us can could should would may might must do does did done use used using please make
let lets just also need want like get got put add fix change update run check look see try""".split())
_HOOK_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def _hook_terms(prompt: str, *, max_terms: int) -> list[str]:
    """The distinct content words of a prompt, in order, minus filler.

    A whole prompt is a poor FTS query: it carries verbs and pleasantries that
    match nothing, and the all-terms gate would then never fire. Keep the
    identifiers and nouns, drop the rest, cap the count.
    """
    words: list[str] = []
    specific: list[str] = []
    for token in _HOOK_TOKEN.findall(prompt):
        word = token.casefold()
        if word in _HOOK_STOP or word in words:
            continue
        words.append(word)
        if _is_identifier(token):
            specific.append(word)
    # Identifiers are what an entry is likely to share with the prompt; when
    # the cap bites, keep them ahead of ordinary prose words.
    ordinary = [w for w in words if w not in specific]
    return (specific + ordinary)[:max_terms]


def _is_identifier(token: str) -> bool:
    """snake_case, kebab-case, digits, or CamelCase: a name rather than a word."""
    return ("_" in token or "-" in token or any(c.isdigit() for c in token)
            or any(c.isupper() for c in token[1:]))


def _run_hook_context(store: KnowledgeStore, args: Namespace) -> int:
    """UserPromptSubmit hook body. Never fails the prompt: any problem exits 0 silently."""
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        return EXIT_OK
    # Claude Code sends `user_prompt`; Codex sends `prompt`. Same contract otherwise.
    prompt = str(event.get("user_prompt") or event.get("prompt") or "")
    cwd = str(event.get("cwd") or getattr(args, "workspace", None) or Path.cwd())
    terms = _hook_terms(prompt, max_terms=max(1, args.max_terms))
    identifiers = {t.casefold() for t in _HOOK_TOKEN.findall(prompt) if _is_identifier(t)}
    if len(terms) < max(1, args.min_terms):
        return EXIT_OK
    prefer = Path(cwd).name
    min_terms = max(1, args.min_terms)
    # Three tries, each still requiring every term of the query to match: the
    # whole prompt; its identifiers alone; then the identifiers plus the three
    # longest ordinary words. A long prompt rarely has every word in one entry,
    # but its names and its rarest words usually do.
    specific = [t for t in terms if t in identifiers]
    longest = sorted((t for t in terms if t not in identifiers and len(t) >= 5), key=len, reverse=True)[:3]
    candidates = [terms, specific, specific + longest]
    tried: set[tuple[str, ...]] = set()
    entries: list[KnowledgeEntry] = []
    for query_terms in candidates:
        key = tuple(query_terms)
        if len(query_terms) < min_terms or key in tried:
            continue
        tried.add(key)
        try:
            entries = store.search(" ".join(query_terms), limit=max(1, args.max_rows), prefer_repo=prefer,
                                   require_all_terms=True)
        except Exception:
            return EXIT_OK
        if entries:
            terms = list(query_terms)
            break
    if not entries:
        return EXIT_OK
    lines = ["WaterFree knowledge entries matching every key term of this prompt "
             "(run `waterfree knowledge get <id>` for one in full):"]
    for e in entries:
        desc = " ".join(e.description.split())
        if len(desc) > 240:
            desc = desc[:240].rstrip() + "…"
        lines.append(f"- [{e.source_repo}] {e.title} (id {e.id}): {desc}")
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        },
        # Not part of the hook contract; lets the usage log count hits like a search.
        "entries": [{"id": e.id} for e in entries],
        "total": len(entries),
        "terms": terms,
    }
    emit_json(payload)
    return EXIT_OK


# ── add-time quality gate ────────────────────────────────────────────────────
# Evidence (docs/20_HARNESS_HELPERS_RESEARCH.md): abstract insights transfer,
# raw traces cause negative transfer, and near-duplicates fill the budget.
# Only a near-duplicate title blocks (exit 2, unless --force); every other
# warning is advisory and the entry is saved anyway, because the agent filing
# the entry is the one who knows whether the warning is right.
#
# A trace is first-person narration of what was done ("I ran X, then I tried
# Y"). Numbered lists are deliberately not a marker: an imperative how-to
# ("1. Set X. 2. Run Y.") is the shape a good lesson takes, and counting list
# numbers called every two-step recipe a log (c60ef78f, 83f80bad). Each
# narrating pronoun counts once however many patterns it matches; two trip it.
_NARRATIVE_VERBS = (
    "ran|re-?ran|tried|attempted|checked|opened|added|changed|removed|saw|noticed|fixed|found|"
    "looked|moved|replaced|reverted|rewrote|wrote|debugged|discovered|reali[sz]ed|updated|deleted|"
    "created|tested|switched|spent|started|restarted|traced|confirmed|verified|reproduced|"
    "investigated|inspected|patched|edited|compared|measured|launched|got|had|did|went|made|took|used"
)
_NARRATING_ACTION = re.compile(
    rf"\b(?:I|we)(?:\s+(?:then|also|first|finally|next|eventually|again|just))?\s+(?:{_NARRATIVE_VERBS})\b",
    re.IGNORECASE,
)
_NARRATING_SEQUENCE = re.compile(
    r"\b(?:then|next|after that|afterwards|finally|first|eventually),?\s+(I|we)\b", re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](?=\s)|\n")
_TRACE_MIN_HITS = 2
_MAX_EVIDENCE = 3
_EVIDENCE_CHARS = 200
_RECENT_TWIN_MINUTES = 15


def _trace_evidence(text: str) -> list[str]:
    """The sentences that narrate, when there are enough of them to call the text a log."""
    pronouns: set[int] = set()
    for match in _NARRATING_ACTION.finditer(text or ""):
        pronouns.add(match.start())
    for match in _NARRATING_SEQUENCE.finditer(text or ""):
        pronouns.add(match.start(1))
    if len(pronouns) < _TRACE_MIN_HITS:
        return []
    bounds: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        bounds.append((start, match.end()))
        start = match.end()
    bounds.append((start, len(text)))
    evidence: list[str] = []
    for position in sorted(pronouns):
        for lo, hi in bounds:
            if lo <= position < hi:
                sentence = " ".join(text[lo:hi].split())
                if len(sentence) > _EVIDENCE_CHARS:
                    sentence = sentence[:_EVIDENCE_CHARS].rstrip() + "…"
                if sentence not in evidence:
                    evidence.append(sentence)
                break
    return evidence[:_MAX_EVIDENCE]


def _looks_like_a_trace(text: str) -> bool:
    """A narrative of what someone did, rather than what to do."""
    return bool(_trace_evidence(text))


def _minutes_since(stamp: str) -> float | None:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 60.0


def _recent_own_twin(twins: list[KnowledgeEntry], source_repo: str) -> tuple[KnowledgeEntry, float] | None:
    """A twin the same repo filed in the last few minutes: almost always this agent's own first try."""
    key = _repo_key(source_repo)
    if not key:
        return None
    for twin in twins:
        if _repo_key(twin.source_repo) != key:
            continue
        minutes = _minutes_since(twin.created_at)
        if minutes is not None and -1.0 <= minutes <= _RECENT_TWIN_MINUTES:
            return twin, max(0.0, minutes)
    return None


def _ago(minutes: float) -> str:
    return "moments ago" if minutes < 2 else f"{int(minutes)} minutes ago"


def _quality_warnings(store: KnowledgeStore, *, title: str, description: str,
                      hierarchy_path: str, prefer_repo: str, source_repo: str = "") -> list[dict]:
    warnings: list[dict] = []
    # Content words only: "on", "the", "when" would otherwise let a stopword
    # decide whether two titles are the same lesson.
    title_terms = _hook_terms(title, max_terms=12)
    twins: list[KnowledgeEntry] = []
    if len(title_terms) >= 2:
        try:
            twins = store.search(" ".join(title_terms), limit=3, prefer_repo=prefer_repo, scope="all",
                                 require_all_terms=True)
        except Exception:
            twins = []
    if twins:
        warning = {
            "kind": "near_duplicate",
            "message": "an existing entry already contains every word of this title; "
                       "if it is the same lesson, revise it with `knowledge update <id>` instead",
            "ids": [e.id for e in twins],
            "titles": [e.title for e in twins],
            # Every one of these title words occurs in each listed entry; that
            # is the whole test, so it is what the agent needs to judge it.
            "shared_terms": title_terms,
        }
        recent = _recent_own_twin(twins, source_repo)
        if recent is not None:
            twin, minutes = recent
            warning["recent_id"] = twin.id
            warning["message"] = (
                f"you saved this as {twin.id} {_ago(minutes)}; use `knowledge update {twin.id}` "
                "to revise it rather than filing a second copy"
            )
        warnings.append(warning)
    if not hierarchy_path:
        warnings.append({
            "kind": "no_hierarchy",
            "message": "no --hierarchy-path: `browse` cannot place this entry; "
                       "give it a stable subject path such as godot/camera or platform/auth/jwt",
        })
    evidence = _trace_evidence(description)
    if evidence:
        warnings.append({
            "kind": "reads_like_a_trace",
            "message": "the description reads like a log of what was done; write the lesson as an "
                       "instruction (do X when Y, because Z) so it transfers to the next session",
            "evidence": evidence,
        })
    return warnings


def _echo(text: str) -> None:
    """Human-facing stderr copy of something the JSON output already carries.

    Written only to an interactive terminal. PowerShell turns any native stderr
    into a NativeCommandError, so a piped call that succeeded read as failed and
    `2>&1 | ConvertFrom-Json` broke (KB-STDERR-001; todos made the same change
    in TODO-STDERR-001). Errors that come with a non-zero exit still use
    emit_error and always reach stderr.
    """
    try:
        if sys.stderr.isatty():
            sys.stderr.write(text)
    except (AttributeError, ValueError, OSError):
        pass


def _write_warnings(warnings: list[dict], *, suffix: str = "") -> None:
    """Each warning echoed for a human, with what tripped it indented underneath."""
    for warning in warnings:
        label = warning["kind"].replace("_", "-")
        tail = f"; {suffix}" if suffix else ""
        _echo(f"warning ({label}): {warning['message']}{tail}\n")
        for entry_id, title in zip(warning.get("ids", []), warning.get("titles", [])):
            _echo(f"    {entry_id}  {title}\n")
        if warning.get("shared_terms"):
            _echo(f"    shared title words: {' '.join(warning['shared_terms'])}\n")
        for sentence in warning.get("evidence", []):
            _echo(f"    > {sentence}\n")


def _read_hook_event() -> dict | None:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        return None
    return event if isinstance(event, dict) else None


def _hook_output(event_name: str, text: str) -> None:
    emit_json({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}})


def _run_hook_session(store: KnowledgeStore, args: Namespace) -> int:
    """SessionStart: a two-line orientation so the store exists in the agent's plan."""
    event = _read_hook_event()
    if event is None:
        return EXIT_OK
    cwd = str(event.get("cwd") or getattr(args, "workspace", None) or Path.cwd())
    repo = Path(cwd).name
    try:
        counts = store.scope_counts()
        total = sum(v for k, v in counts.items() if k != "assets")
        if not total:
            return EXIT_OK
        own = store.entries_for_repo(repo)
        picks = _most_retrieved(store, own, limit=max(1, args.max_rows))
    except Exception:
        return EXIT_OK
    lines = [
        f"WaterFree knowledge: {total} shared lessons available ({len(own)} filed from {repo}). "
        "Before reinventing a pattern or after a fix that took more than two tries: "
        "`waterfree knowledge search \"<words>\"` / `waterfree knowledge add`."
    ]
    for entry in picks:
        lines.append(f"- {entry.title} (id {entry.id})")
    _hook_output("SessionStart", "\n".join(lines))
    return EXIT_OK


def _most_retrieved(store: KnowledgeStore, candidates: list[KnowledgeEntry], *, limit: int) -> list[KnowledgeEntry]:
    """The project's entries agents actually come back to; newest ones when nothing has been retrieved yet."""
    if not candidates:
        return []
    from backend.cli import usage_log
    from backend.knowledge.consolidate import retrieval_counts

    paths = [usage_log.transcript_log_path()]
    live = usage_log.log_path()
    if live is not None:
        paths.insert(0, live)
    counts, _ = retrieval_counts(usage_log.read_records(paths), since=None,
                                 known_ids=[e.id for e in candidates])
    # most retrieved first; among equals, the most recently written
    ranked = sorted(candidates, key=lambda e: (counts.get(e.id, 0), e.updated_at or e.created_at), reverse=True)
    return ranked[:limit]


# Phrases that mark a hard-won lesson in a final message. Deliberately narrow:
# a reminder that fires on every turn is noise, and noise gets muted.
_LESSON_MARKERS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("several attempts", re.compile(
        r"\b(?:after|took|on the) (?:two|three|four|five|several|\d+)(?:th|rd|nd)? (?:attempts?|tries|rounds?|iterations?)\b",
        re.IGNORECASE)),
    ("workaround", re.compile(r"\bwork(?:ed)?[- ]?around\b", re.IGNORECASE)),
    ("root cause", re.compile(r"\broot cause\b", re.IGNORECASE)),
    ("turned out", re.compile(r"\bturned out (?:to be|that)\b", re.IGNORECASE)),
    ("real cause", re.compile(r"\b(?:the )?(?:real|actual) (?:problem|bug|issue|cause) was\b", re.IGNORECASE)),
    ("gotcha", re.compile(r"\bgotcha\b", re.IGNORECASE)),
    ("did not work", re.compile(r"\b(?:did not|didn't|does not|doesn't) work(?:,| because| since| as expected)", re.IGNORECASE)),
)
_ALREADY_FILED = re.compile(r"waterfree knowledge (?:add|update)\b|knowledge[- ]base entry|filed (?:it|this|a lesson)", re.IGNORECASE)


def lesson_markers(text: str) -> list[str]:
    """Labels of the lesson phrases present in a final message."""
    return [label for label, pattern in _LESSON_MARKERS if pattern.search(text or "")]


def _run_hook_stop(store: KnowledgeStore, args: Namespace) -> int:
    """Stop: once per turn, ask for the lesson when the message says there was one."""
    event = _read_hook_event()
    if event is None or event.get("stop_hook_active"):
        return EXIT_OK
    message = str(event.get("last_assistant_message") or "")
    if len(message) < 80 or _ALREADY_FILED.search(message):
        return EXIT_OK
    hits = lesson_markers(message)
    if not hits:
        return EXIT_OK
    cwd = str(event.get("cwd") or getattr(args, "workspace", None) or Path.cwd())
    repo = Path(cwd).name
    reason = (
        "WaterFree reminder: that reads like a hard-won lesson. If it took more than two attempts "
        "or exposed a non-obvious cause, file the insight (not the log) so the next session starts "
        "from it: `waterfree knowledge search \"<key words>\"` first, then "
        f"`waterfree knowledge add --title \"<do X when Y>\" --description \"<why, and what to do>\" "
        f"--context \"<when not to>\" --source-repo {repo} --hierarchy-path <area/topic> --tag <t>`. "
        "If it is not worth keeping, say so in one line and stop."
    )
    emit_json({
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": reason},
        "markers": hits,
    })
    return EXIT_OK


_STDIN_FLAGS = (("--code-file", "code_file"), ("--description-file", "description_file"),
                ("--context-file", "context_file"))


def _check_one_stdin_reader(args: Namespace) -> None:
    readers = [flag for flag, attr in _STDIN_FLAGS if getattr(args, attr, None) == "-"]
    if len(readers) > 1:
        raise ValueError(f"{' and '.join(readers)} cannot both read stdin in one command; use a file for all but one")


def _read_text_file(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _read_code(args: Namespace) -> str | None:
    """The code body from --code/--code-file, or None when neither was given."""
    if args.code is not None:
        return args.code
    if args.code_file is None:
        return None
    return _read_text_file(args.code_file)


def _read_description(args: Namespace) -> str | None:
    path = getattr(args, "description_file", None)
    if path is None:
        return args.description
    return _read_text_file(path)


def _read_context(args: Namespace) -> str | None:
    path = getattr(args, "context_file", None)
    if path is None:
        return args.context
    return _read_text_file(path)


def _append_paragraph(existing: str, addition: str) -> str:
    base = (existing or "").rstrip()
    return f"{base}\n\n{addition.strip()}" if base else addition.strip()


def _discarded_fraction(old: str, new: str) -> float:
    """How much of the old text (by words) does not survive into the new one."""
    old_words = old.split()
    if not old_words:
        return 0.0
    matcher = difflib.SequenceMatcher(None, old_words, new.split(), autojunk=False)
    kept = sum(block.size for block in matcher.get_matching_blocks())
    return 1.0 - kept / len(old_words)


def _replace_discard_warning(field: str, old: str, new: str, *, entry: KnowledgeEntry,
                             append_flag: str | None) -> str | None:
    """Mirror of `todos --ai-notes`: the stderr line when a replace throws most of the old text away.

    Measured on words that survive, not on length, so a same-length rewrite
    that keeps nothing still warns.
    """
    if not (old or "").strip():
        return None
    lost = _discarded_fraction(old, new)
    if lost <= 0.5:
        return None
    keep = f"use --{append_flag} to add to it instead; " if append_flag else ""
    return (
        f"warning: this replace discards {round(lost * 100)}% of the old {field} "
        f"({len(old)} -> {len(new)} characters); {keep}the old text is kept: "
        f"`knowledge history {entry.id} --field {field}` shows it and "
        f"`knowledge update {entry.id} --restore-revision {entry.revision}` puts it back"
    )


def _emit_ambiguous(exc: AmbiguousEntryError) -> int:
    lines = [str(exc)] + [f"  {entry_id}  {title}" for entry_id, title in exc.candidates]
    return emit_error("\n".join(lines), exit_code=EXIT_USAGE)


def _resolve(store: KnowledgeStore, ref: str, *, allow_title: bool = True) -> tuple[KnowledgeEntry | None, int | None]:
    """(entry, None) or (None, exit code after printing why)."""
    try:
        entry = store.get_entry(ref, allow_title=allow_title)
    except AmbiguousEntryError as exc:
        return None, _emit_ambiguous(exc)
    except ValueError as exc:
        return None, emit_error(str(exc), exit_code=EXIT_USAGE)
    if entry is None:
        return None, emit_error(f"Entry {ref} not found.", exit_code=EXIT_NOT_FOUND)
    return entry, None


def _workspace_dir(args: Namespace) -> str:
    return str(getattr(args, "workspace", None) or Path.cwd())


def _prefer_repo(args: Namespace) -> str:
    return Path(args.workspace).name if getattr(args, "workspace", None) else Path.cwd().name


def _path_hint(store: KnowledgeStore, path: str) -> tuple[str, list[dict]]:
    suggestions = store.nearest_hierarchy_paths(path)
    if suggestions:
        nearest = ", ".join(f"{s['path']} ({s['entry_count']})" for s in suggestions)
        return f"No taxonomy node '{path}'. Nearest: {nearest}.", suggestions
    return (f"No taxonomy node '{path}' and nothing close; `knowledge browse --depth 1` lists the "
            "top-level nodes."), []


def run(args: Namespace) -> int:
    if args.action == "hook-pretool":
        # Runs before every Bash call; it opens the store only when it fires.
        from backend.cli import knowledge_pretool  # noqa: PLC0415

        return knowledge_pretool.run(lesson_lookup=knowledge_pretool._lesson_title)[0]
    store = KnowledgeStore()
    try:
        return _run_store(args, store)
    finally:
        store.close()


def _run_search(store: KnowledgeStore, args: Namespace) -> int:
    prefer = _prefer_repo(args)
    entries = store.search(args.query, limit=args.limit, repo=args.repo, prefer_repo=prefer,
                           scope=args.scope, path=args.path)
    mode = "brief" if args.brief else "full" if args.full else "summary"
    shaper = _RowShaper(_workspace_dir(args)) if mode != "brief" else None
    if mode == "brief":
        payload = [_entry_brief(e) for e in entries]
    else:
        shape = shaper.full if mode == "full" else shaper.summary
        payload = [shape(e) for e in entries]
    envelope = {"entries": payload, "total": len(payload), "preferred_repo": prefer, "scope": args.scope,
                "mode": mode}
    if not payload:
        envelope["hint"] = (
            "0 entries match. Search is global across every project; try fewer or "
            "different words, or `browse` a taxonomy path."
        )
        path_key = normalize_hierarchy_path(args.path)
        if path_key and path_key not in store.hierarchy_node_counts():
            hint, suggestions = _path_hint(store, path_key)
            envelope["hint"] = f"{hint} {envelope['hint']}"
            envelope["suggestions"] = suggestions
    elif mode == "summary":
        envelope["hint"] = "Rows omit code/context; `knowledge get <id>` returns one entry in full."
    emit_json(envelope, compact=mode == "brief")
    return EXIT_OK


def _run_browse(store: KnowledgeStore, args: Namespace) -> int:
    path = args.path
    if args.path_arg is not None:
        if args.path and normalize_hierarchy_path(args.path) != normalize_hierarchy_path(args.path_arg):
            return emit_error(f"two different paths given ({args.path_arg!r} and --path {args.path!r}); pass one",
                              exit_code=EXIT_USAGE)
        path = args.path_arg
    shaper = _RowShaper(_workspace_dir(args))
    payload = store.browse_hierarchy(
        path=path,
        depth=args.depth,
        include_entries=args.include_entries,
        entry_limit=args.entry_limit,
        list_entries=args.list,
        entry_formatter=shaper.full if args.full else shaper.summary,
    )
    if payload["path"]:
        if payload["entry_count"] == 0:
            payload["hint"], payload["suggestions"] = _path_hint(store, payload["path"])
        else:
            twins = store.separator_twins(payload["path"])
            if twins:
                payload["see_also"] = twins
    emit_json(payload)
    return EXIT_OK


def _run_get(store: KnowledgeStore, args: Namespace) -> int:
    entry, failed = _resolve(store, args.entry_id)
    if entry is None:
        return failed
    note = _RowShaper(_workspace_dir(args)).note(entry)
    if args.text:
        head = f"{entry.title}\n({note})" if note else entry.title
        emit_raw(f"{head}\n\n{entry.description}\n\n{entry.code}\n\n{entry.context}")
    else:
        row = _entry_to_dict(entry)
        if note:
            row["source_note"] = note
        emit_json(row)
    return EXIT_OK


def _run_history(store: KnowledgeStore, args: Namespace) -> int:
    try:
        entry_id, entry, rows = store.history(args.entry_id)
    except AmbiguousEntryError as exc:
        return _emit_ambiguous(exc)
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)
    except KeyError:
        return emit_error(f"Entry {args.entry_id} not found, and no history is recorded for it.",
                          exit_code=EXIT_NOT_FOUND)
    if args.field:
        rows = [r for r in rows if r["field"] in args.field]
    total = len(rows)
    rows = rows[:max(0, args.limit)]
    clip = 400
    shaped = []
    for row in rows:
        value = row["old_value"]
        item = {"revision": row["revision"], "field": row["field"], "change": row["change"],
                "changed_at": row["changed_at"], "old_chars": len(value)}
        if args.full or len(value) <= clip:
            item["old_value"] = value
        else:
            item["old_value"] = value[:clip].rstrip() + "…"
            item["clipped"] = True
        shaped.append(item)
    payload = {
        "id": entry_id,
        "title": entry.title if entry else None,
        "revision": entry.revision if entry else None,
        "deleted": entry is None,
        "history": shaped,
        "total": total,
        "returned": len(shaped),
    }
    if entry is None:
        payload["hint"] = ("The entry was deleted; rows with change=delete hold its last values "
                           "(`--full` for whole bodies). Re-file it with `knowledge add`.")
    elif shaped:
        payload["hint"] = (f"Each old_value is what the field held at that revision. "
                           f"`knowledge update {entry_id} --restore-revision N` puts revision N back "
                           "(add --restore-field F to restore only some fields).")
    else:
        payload["hint"] = "No recorded changes: history starts with the first update made after it was introduced."
    emit_json(payload)
    return EXIT_OK


def _run_update(store: KnowledgeStore, args: Namespace) -> int:
    current, failed = _resolve(store, args.entry_id)
    if current is None:
        return failed
    field_flags = ("title", "description", "description_file", "append_description", "code", "code_file",
                   "snippet_type", "tag", "context", "context_file", "append_context", "source_repo",
                   "source_file", "source_repo_url", "hierarchy_path", "scope")
    if args.restore_revision is not None:
        given = [f"--{name.replace('_', '-')}" for name in field_flags if getattr(args, name, None) is not None]
        if given:
            return emit_error(f"--restore-revision cannot be combined with {', '.join(given)}; "
                              "restore first, then update", exit_code=EXIT_USAGE)
        try:
            entry, restored = store.restore_revision(current.id, args.restore_revision, fields=args.restore_field)
        except DuplicateContentError as exc:
            return emit_error(str(exc), exit_code=EXIT_USAGE)
        except ValueError as exc:
            return emit_error(str(exc), exit_code=EXIT_USAGE)
        return _emit_update(entry, restored, args,
                            message=f"Entry '{entry.title}' restored to revision {args.restore_revision} "
                                    f"(now revision {entry.revision}).")
    if args.restore_field:
        return emit_error("--restore-field needs --restore-revision N", exit_code=EXIT_USAGE)

    try:
        _check_one_stdin_reader(args)
        description = _read_description(args)
        context = _read_context(args)
        code = _read_code(args)
    except (OSError, UnicodeError, ValueError) as exc:
        return emit_error(f"could not read input: {exc}", exit_code=EXIT_USAGE)
    discard_warnings: list[str | None] = []
    if args.append_description is not None:
        description = _append_paragraph(current.description, args.append_description)
    elif description is not None:
        discard_warnings.append(_replace_discard_warning("description", current.description, description,
                                                         entry=current, append_flag="append-description"))
    if args.append_context is not None:
        context = _append_paragraph(current.context, args.append_context)
    elif context is not None:
        discard_warnings.append(_replace_discard_warning("context", current.context, context, entry=current,
                                                         append_flag="append-context"))
    if code is not None:
        discard_warnings.append(_replace_discard_warning("code", current.code, code, entry=current,
                                                         append_flag=None))
    fields = {
        "title": args.title,
        "description": description,
        "code": code,
        "snippet_type": args.snippet_type,
        "tags": args.tag,
        "context": context,
        "source_repo": args.source_repo,
        "source_file": args.source_file,
        "source_repo_url": args.source_repo_url,
        "hierarchy_path": args.hierarchy_path,
        "scope": args.scope,
    }
    changes = {k: v for k, v in fields.items() if v is not None}
    if not changes:
        return emit_error(
            "nothing to update: pass at least one of --title, --description(-file), --append-description, "
            "--code, --code-file, --snippet-type, --tag, --context(-file), --append-context, --source-repo, "
            "--source-file, --source-repo-url, --hierarchy-path, --scope, or --restore-revision N",
            exit_code=EXIT_USAGE,
        )
    if args.source_file is not None:
        # {} clears a stale record when the new file is outside git (or empty).
        changes["source_ref"] = capture_source_ref(args.source_file, _workspace_dir(args)) or {}
    try:
        entry = store.update_entry(current.id, **changes)
    except KeyError:
        return emit_error(f"Entry {args.entry_id} not found.", exit_code=EXIT_NOT_FOUND)
    except DuplicateContentError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)
    notes = [n for n in (_normalized_path_note(args.hierarchy_path, entry.effective_hierarchy_path()),) if n]
    if args.source_repo is not None:
        store.upsert_repo(args.source_repo, args.source_file or args.source_repo,
                          args.source_repo_url or "")
    # Only now: a rejected update keeps the old text, so warning earlier would mislead.
    warnings = [w for w in discard_warnings if w]
    for warning in warnings:
        _echo(warning + "\n")
    if args.source_file is not None:
        notes += [n for n in (_note_uncommitted(entry),) if n]
    _emit_notes(notes)
    reported = sorted(k for k in changes if k != "source_ref" or entry.source_ref)
    return _emit_update(entry, reported, args, message=f"Entry '{entry.title}' revised (revision {entry.revision}).",
                        warnings=warnings, notes=notes)


def _emit_update(entry: KnowledgeEntry, updated: list[str], args: Namespace, *, message: str,
                 warnings: list[str] | None = None, notes: list[str] | None = None) -> int:
    extra = {k: v for k, v in (("warnings", warnings), ("notes", notes)) if v}
    if args.quiet:
        receipt = {"id": entry.id, "updated": updated, "revision": entry.revision}
        if warnings:
            receipt["warnings"] = len(warnings)
        emit_json(receipt, compact=True)
    else:
        emit_json({**_entry_to_dict(entry), "updated": updated, "message": message, **extra})
    return EXIT_OK


def _note_uncommitted(entry: KnowledgeEntry) -> str | None:
    ref = entry.source_ref or {}
    if ref and ref.get("committed") is False:
        return (
            f"{ref.get('path')} is {ref.get('status')} in worktree {ref.get('worktreePath')} "
            f"(branch {ref.get('branch') or '?'}); other agents will not have it until it is committed. "
            "Search rows will say so."
        )
    return None


def _normalized_path_note(given: str | None, normalized: str) -> str | None:
    if given and given.replace("\\", "/").strip("/") != normalized:
        return f"hierarchy path normalized to lowercase: {normalized}"
    return None


def _emit_notes(notes: list[str]) -> None:
    for note in notes:
        _echo(f"note: {note}\n")


def _run_add(store: KnowledgeStore, args: Namespace) -> int:
    try:
        _check_one_stdin_reader(args)
        args.description = _read_description(args)
        context = _read_context(args) or ""
        code = _read_code(args)
    except (OSError, UnicodeError, ValueError) as exc:
        return emit_error(f"could not read input: {exc}", exit_code=EXIT_USAGE)
    snippet_type = args.snippet_type
    if code is None:
        code = ""
        snippet_type = snippet_type or "convention"
    elif snippet_type is None:
        return emit_error(
            "--snippet-type is required when --code/--code-file is given "
            f"(one of: {', '.join(_SNIPPET_TYPES)})",
            exit_code=EXIT_USAGE,
        )
    source_ref = capture_source_ref(args.source_file, _workspace_dir(args)) if args.source_file else None
    entry = KnowledgeEntry.create(
        source_repo=args.source_repo,
        source_file=args.source_file,
        snippet_type=snippet_type,
        title=args.title,
        description=args.description,
        code=code,
        tags=args.tag,
        context=context,
        source_repo_url=args.source_repo_url,
        hierarchy_path=args.hierarchy_path or None,
        scope=args.scope,
        source_ref=source_ref,
    )
    normalized_path = entry.effective_hierarchy_path()
    notes = [n for n in (_normalized_path_note(args.hierarchy_path, normalized_path),) if n]
    warnings: list[dict] | None = None
    if not args.no_check:
        warnings = _quality_warnings(
            store, title=args.title, description=args.description,
            hierarchy_path=args.hierarchy_path, prefer_repo=_prefer_repo(args), source_repo=args.source_repo,
        )
        near = next((w for w in warnings if w["kind"] == "near_duplicate"), None)
        if near is not None and not args.force and not store.has_content_hash(entry.content_hash):
            return _emit_blocked(near, warnings, args)
    if getattr(args, "dry_run", False):
        if warnings:
            _write_warnings(warnings, suffix="dry run: not saved")
            _echo(f"dry run: not saved ({_count(warnings, 'warning')})\n")
        emit_json({"added": False, "id": None, "saved": False, "dry_run": True,
                   "summary": "dry run: not saved", "entry": _entry_to_dict(entry), "warnings": warnings or []})
        return EXIT_OK

    added = store.add_entry(entry)
    store.upsert_repo(args.source_repo, args.source_file or args.source_repo, args.source_repo_url)
    stored = entry if added else store.get_by_content_hash(entry.content_hash)
    stored_id = stored.id if stored is not None else None
    revision = stored.revision if stored is not None else None
    if added:
        summary = f"saved: {entry.id}" + (f" ({_count(warnings, 'warning')})" if warnings else "")
        suffix = f"saved anyway as {entry.id}; revise with `knowledge update {entry.id}`"
        message = f"Entry '{args.title}' added to knowledge store."
    elif stored is not None:
        summary = (f"not saved: identical content is already entry {stored.id} (revision {stored.revision}); "
                   f"revise it with `knowledge update {stored.id}`")
        suffix = f"not saved: identical content is already entry {stored.id}"
        message = f"Entry '{args.title}' already exists as {stored.id} (duplicate content, revision {stored.revision})."
    else:  # pragma: no cover - the twin vanished between insert and lookup
        summary = "not saved: identical content already exists"
        suffix = "not saved"
        message = f"Entry '{args.title}' already exists (duplicate content; skipped)."
    if warnings:
        _write_warnings(warnings, suffix=suffix)
        notes.append(suffix)
    if added:
        notes += [n for n in (_note_uncommitted(entry),) if n]
    _emit_notes([n for n in notes if n != suffix])
    if warnings or not added:
        _echo(summary + "\n")
    if args.quiet:
        receipt = {"id": stored_id, "added": added, "revision": revision}
        if warnings:
            receipt["warnings"] = [w["kind"] for w in warnings]
        emit_json(receipt, compact=True)
        return EXIT_OK
    payload = {
        "added": added,
        "id": stored_id,
        "saved": stored is not None,
        "summary": summary,
        "revision": revision,
        "snippet_type": snippet_type,
        "scope": entry.scope,
        "hierarchy_path": normalized_path,
        "message": message,
    }
    if not added and stored is not None:
        payload["duplicate_of"] = stored.id
    if entry.source_ref and added:
        payload["source_ref"] = entry.source_ref
    if notes:
        payload["notes"] = notes
    if warnings is not None:
        payload["warnings"] = warnings
    emit_json(payload)
    return EXIT_OK


def _count(items: list | None, noun: str) -> str:
    n = len(items or [])
    kinds = ", ".join(w["kind"] for w in items or [] if isinstance(w, dict) and "kind" in w)
    return f"{n} {noun}{'' if n == 1 else 's'}" + (f": {kinds}" if kinds else "")


def _emit_blocked(near: dict, warnings: list[dict], args: Namespace) -> int:
    recent = near.get("recent_id")
    if recent:
        summary = f"not saved: you saved this as {recent} moments ago; use `knowledge update {recent}`"
        message = near["message"]
    else:
        ids = ", ".join(near["ids"])
        summary = f"not saved: near-duplicate of {ids}; use `knowledge update <id>` or pass --force after review"
        message = "near-duplicate title; use `knowledge update <id>` or pass --force after review"
    _write_warnings(warnings)
    _echo(summary + "\n")
    if args.quiet:
        emit_json({"id": None, "added": False, "blocked": True, "duplicate_of": near["ids"]}, compact=True)
        return EXIT_USAGE
    payload = {"added": False, "id": None, "saved": False, "blocked": True, "summary": summary,
               "message": message, "duplicate_of": near["ids"]}
    if recent:
        payload["recent_id"] = recent
    payload["warnings"] = warnings
    emit_json(payload)
    return EXIT_USAGE


def _run_delete(store: KnowledgeStore, args: Namespace) -> int:
    # Titles are not accepted here: deleting by a title someone else may
    # share is too easy a mistake to make with a destructive command.
    entry, failed = _resolve(store, args.entry_id, allow_title=False)
    if entry is None:
        if failed == EXIT_NOT_FOUND:
            emit_json({"deleted": False, "message": f"Entry {args.entry_id} not found."})
        return failed
    deleted = store.delete_entry(entry.id)
    emit_json({
        "deleted": deleted,
        "id": entry.id,
        "title": entry.title,
        "message": (f"Entry {entry.id} deleted; `knowledge history {entry.id}` still shows its fields."
                    if deleted else f"Entry {entry.id} not found."),
    })
    return EXIT_OK if deleted else EXIT_NOT_FOUND


def _run_store(args: Namespace, store: KnowledgeStore) -> int:
    action = args.action

    if action == "search":
        return _run_search(store, args)

    if action == "browse":
        return _run_browse(store, args)

    if action == "get":
        return _run_get(store, args)

    if action == "history":
        return _run_history(store, args)

    if action == "update":
        return _run_update(store, args)

    if action == "add":
        return _run_add(store, args)

    if action == "delete":
        return _run_delete(store, args)

    if action == "hook-context":
        return _run_hook_context(store, args)

    if action == "hook-session":
        return _run_hook_session(store, args)

    if action == "hook-stop":
        return _run_hook_stop(store, args)

    if action == "consolidate":
        from backend.knowledge import consolidate as consolidation

        chat_fn = None
        no_model_note = ""
        if not args.no_llm:
            try:
                chat_fn = consolidation.ollama_chat_fn(
                    workspace_path=str(getattr(args, "workspace", "") or ""), model=args.model,
                )
            except Exception as exc:
                no_model_note = f"no local model for merge proposals ({exc}); reporting groups without proposals"
                _echo(f"note: {no_model_note}\n")
        report = consolidation.consolidate(
            store,
            window_days=max(1, args.window),
            min_age_days=max(0, args.min_age),
            duplicate_threshold=args.threshold,
            max_group_size=max(2, args.max_group_size),
            include_assets=args.include_assets,
            chat=chat_fn,
            apply=args.apply,
            max_groups=max(0, args.max_groups),
        )
        payload = report.to_dict()
        if no_model_note:
            payload["notes"] = [no_model_note]
        if args.report_file:
            Path(args.report_file).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            payload["report_file"] = args.report_file
        emit_json(payload)
        return EXIT_OK

    if action == "list-sources":
        repos = store.list_repos()
        emit_json({
            "total_entries": store.total_entries(),
            "sources": [_repo_to_dict(r) for r in repos],
        })
        return EXIT_OK

    if action == "stats":
        repos = store.list_repos()
        hierarchy = store.browse_hierarchy(depth=args.depth)
        emit_json({
            "total_entries": store.total_entries(),
            "by_scope": store.scope_counts(),
            "source_count": len(repos),
            "top_level_category_count": len(hierarchy["nodes"]),
            "hierarchy": hierarchy["nodes"],
        })
        return EXIT_OK

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)
