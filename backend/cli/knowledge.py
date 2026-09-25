"""`waterfree knowledge ...` — global knowledge store CLI."""

from __future__ import annotations

import json
import re
import sys
from argparse import Namespace, _SubParsersAction
from pathlib import Path

from backend.cli._common import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    add_full_arg,
    add_workspace_arg,
    emit_error,
    emit_json,
)
from backend.knowledge.models import SCOPES, KnowledgeEntry
from backend.knowledge.store import SEARCH_SCOPES, DuplicateContentError, KnowledgeStore

_SNIPPET_TYPES = ("pattern", "utility", "style", "api_usage", "convention")


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
    p_search.add_argument("--repo", default="",
                          help="Only entries from this source repo (name or path).")
    p_search.add_argument(
        "--scope", default="default", choices=SEARCH_SCOPES,
        help="default = global entries plus the current workspace's own project entries; "
             "all; or exactly global / project / assets.",
    )
    p_search.add_argument(
        "--full", action="store_true",
        help="Include each entry's code and context. Default rows carry the id, "
             "title, description, type, repo, tags and sizes only; use `get <id>` "
             "for one entry's body.",
    )

    p_browse = actions.add_parser("browse", help="Walk the knowledge taxonomy")
    add_workspace_arg(p_browse)
    p_browse.add_argument("--path", default="")
    p_browse.add_argument("--depth", type=int, default=2)
    p_browse.add_argument("--include-entries", action="store_true")
    p_browse.add_argument("--entry-limit", type=int, default=10)
    add_full_arg(p_browse)

    p_get = actions.add_parser("get", help="Fetch one knowledge entry by id")
    add_workspace_arg(p_get)
    p_get.add_argument("entry_id")

    p_add = actions.add_parser("add", help="Add a knowledge entry")
    add_workspace_arg(p_add)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--description", required=True)
    code_group = p_add.add_mutually_exclusive_group()
    code_group.add_argument("--code", help="Inline code body (small snippets only)")
    code_group.add_argument("--code-file",
                            help="Read code body from a file. Use '-' for stdin.")
    p_add.add_argument(
        "--snippet-type", default=None, choices=_SNIPPET_TYPES,
        help="Required when code is given; defaults to 'convention' for a prose-only entry.",
    )
    p_add.add_argument("--source-repo", required=True)
    p_add.add_argument("--source-file", default="")
    p_add.add_argument("--tag", action="append", default=[],
                       help="Repeatable. e.g. --tag python --tag retry")
    p_add.add_argument("--context", default="")
    p_add.add_argument("--source-repo-url", default="")
    p_add.add_argument("--hierarchy-path", default="",
                       help="Slash-separated taxonomy path, e.g. platform/auth/jwt")
    p_add.add_argument(
        "--no-check", action="store_true",
        help="Skip the quality gate (near-duplicate title, missing hierarchy path, trace-like description).",
    )
    p_add.add_argument(
        "--scope", default=None, choices=SCOPES,
        help="global (default: any project may see it), project (only searches from its own "
             "source repo), assets (catalog row; the default for assets/* paths).",
    )

    p_update = actions.add_parser(
        "update",
        help="Revise an entry in place. The id is kept and 'revision' is bumped; "
             "only the fields you pass change.",
    )
    add_workspace_arg(p_update)
    p_update.add_argument("entry_id")
    p_update.add_argument("--title", default=None)
    p_update.add_argument("--description", default=None)
    update_code = p_update.add_mutually_exclusive_group()
    update_code.add_argument("--code", default=None, help="Replace the code body inline.")
    update_code.add_argument("--code-file", default=None,
                             help="Replace the code body from a file. Use '-' for stdin.")
    p_update.add_argument("--snippet-type", default=None, choices=_SNIPPET_TYPES)
    p_update.add_argument("--tag", action="append", default=None,
                          help="Repeatable. Replaces the whole tag list when given.")
    p_update.add_argument("--context", default=None)
    p_update.add_argument("--source-repo", default=None)
    p_update.add_argument("--source-file", default=None)
    p_update.add_argument("--source-repo-url", default=None)
    p_update.add_argument("--hierarchy-path", default=None)
    p_update.add_argument("--scope", default=None, choices=SCOPES)

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

    p_delete = actions.add_parser("delete", help="Remove a knowledge entry by id")
    add_workspace_arg(p_delete)
    p_delete.add_argument("entry_id")

    p_list_sources = actions.add_parser("list-sources", help="List all indexed repos/sources")
    add_workspace_arg(p_list_sources)
    add_full_arg(p_list_sources)

    p_stats = actions.add_parser("stats", help="Summary statistics")
    add_workspace_arg(p_stats)
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
    }


_SUMMARY_DESCRIPTION_CHARS = 320


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
# The gate warns; it never blocks, because the agent filing the entry is the
# one who knows whether the warning is right.
_TRACE_MARKERS = (
    re.compile(r"\bI (?:ran|tried|checked|opened|added|changed|removed|saw|noticed|fixed|then)\b", re.IGNORECASE),
    re.compile(r"\b(?:then|next|after that|finally),? I\b", re.IGNORECASE),
    re.compile(r"^\s*(?:\d+[.)]|step \d+|first,|second,|third,)", re.IGNORECASE | re.MULTILINE),
)


def _looks_like_a_trace(text: str) -> bool:
    """A narrative of what someone did, rather than what to do."""
    hits = sum(len(marker.findall(text)) for marker in _TRACE_MARKERS)
    return hits >= 2


def _quality_warnings(store: KnowledgeStore, *, title: str, description: str,
                      hierarchy_path: str, prefer_repo: str) -> list[dict]:
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
        warnings.append({
            "kind": "near_duplicate",
            "message": "an existing entry already contains every word of this title; "
                       "if it is the same lesson, revise it with `knowledge update <id>` instead",
            "ids": [e.id for e in twins],
            "titles": [e.title for e in twins],
        })
    if not hierarchy_path:
        warnings.append({
            "kind": "no_hierarchy",
            "message": "no --hierarchy-path: `browse` cannot place this entry; "
                       "give it a stable subject path such as godot/camera or platform/auth/jwt",
        })
    if _looks_like_a_trace(description):
        warnings.append({
            "kind": "reads_like_a_trace",
            "message": "the description reads like a log of what was done; write the lesson as an "
                       "instruction (do X when Y, because Z) so it transfers to the next session",
        })
    return warnings


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
    counts, _ = retrieval_counts(usage_log.read_records(paths), since=None)
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


def _read_code(args: Namespace) -> str | None:
    """The code body from --code/--code-file, or None when neither was given."""
    if args.code is not None:
        return args.code
    if args.code_file is None:
        return None
    if args.code_file == "-":
        return sys.stdin.read()
    return Path(args.code_file).read_text(encoding="utf-8")


def run(args: Namespace) -> int:
    store = KnowledgeStore()
    action = args.action

    if action == "search":
        prefer = Path(args.workspace).name if getattr(args, "workspace", None) else Path.cwd().name
        entries = store.search(args.query, limit=args.limit, repo=args.repo, prefer_repo=prefer,
                               scope=args.scope)
        shape = _entry_to_dict if args.full else _entry_summary
        payload = [shape(e) for e in entries]
        envelope = {"entries": payload, "total": len(payload), "preferred_repo": prefer, "scope": args.scope}
        if not payload:
            envelope["hint"] = (
                "0 entries match. Search is global across every project; try fewer or "
                "different words, or `browse` a taxonomy path."
            )
        elif not args.full:
            envelope["hint"] = "Rows omit code/context; `knowledge get <id>` returns one entry in full."
        emit_json(envelope)
        return EXIT_OK

    if action == "browse":
        emit_json(store.browse_hierarchy(
            path=args.path,
            depth=args.depth,
            include_entries=args.include_entries,
            entry_limit=args.entry_limit,
        ))
        return EXIT_OK

    if action == "get":
        entry = store.get_entry(args.entry_id)
        if entry is None:
            return emit_error(f"Entry {args.entry_id} not found.", exit_code=EXIT_NOT_FOUND)
        emit_json(_entry_to_dict(entry))
        return EXIT_OK

    if action == "update":
        try:
            code = _read_code(args)
        except (OSError, FileNotFoundError) as exc:
            return emit_error(f"could not read code: {exc}", exit_code=EXIT_USAGE)
        fields = {
            "title": args.title,
            "description": args.description,
            "code": code,
            "snippet_type": args.snippet_type,
            "tags": args.tag,
            "context": args.context,
            "source_repo": args.source_repo,
            "source_file": args.source_file,
            "source_repo_url": args.source_repo_url,
            "hierarchy_path": args.hierarchy_path,
            "scope": args.scope,
        }
        changes = {k: v for k, v in fields.items() if v is not None}
        if not changes:
            return emit_error(
                "nothing to update: pass at least one of --title, --description, --code, "
                "--code-file, --snippet-type, --tag, --context, --source-repo, --source-file, "
                "--source-repo-url, --hierarchy-path, --scope",
                exit_code=EXIT_USAGE,
            )
        try:
            entry = store.update_entry(args.entry_id, **changes)
        except KeyError:
            return emit_error(f"Entry {args.entry_id} not found.", exit_code=EXIT_NOT_FOUND)
        except DuplicateContentError as exc:
            return emit_error(str(exc), exit_code=EXIT_USAGE)
        if args.source_repo is not None:
            store.upsert_repo(args.source_repo, args.source_file or args.source_repo,
                              args.source_repo_url or "")
        emit_json({
            **_entry_to_dict(entry),
            "updated": sorted(changes),
            "message": f"Entry '{entry.title}' revised (revision {entry.revision}).",
        })
        return EXIT_OK

    if action == "add":
        try:
            code = _read_code(args)
        except (OSError, FileNotFoundError) as exc:
            return emit_error(f"could not read code: {exc}", exit_code=EXIT_USAGE)
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
        entry = KnowledgeEntry.create(
            source_repo=args.source_repo,
            source_file=args.source_file,
            snippet_type=snippet_type,
            title=args.title,
            description=args.description,
            code=code,
            tags=args.tag,
            context=args.context,
            source_repo_url=args.source_repo_url,
            hierarchy_path=args.hierarchy_path or None,
            scope=args.scope,
        )
        warnings: list[dict] | None = None
        if not args.no_check:
            prefer = Path(args.workspace).name if getattr(args, "workspace", None) else Path.cwd().name
            warnings = _quality_warnings(
                store, title=args.title, description=args.description,
                hierarchy_path=args.hierarchy_path, prefer_repo=prefer,
            )
            for warning in warnings:
                label = warning["kind"].replace("_", "-")
                sys.stderr.write(f"warning ({label}): {warning['message']}\n")
                for entry_id, title in zip(warning.get("ids", []), warning.get("titles", [])):
                    sys.stderr.write(f"    {entry_id}  {title}\n")
        added = store.add_entry(entry)
        store.upsert_repo(args.source_repo,
                          args.source_file or args.source_repo,
                          args.source_repo_url)
        payload = {
            "id": entry.id,
            "added": added,
            "snippet_type": snippet_type,
            "scope": entry.scope,
            "hierarchy_path": entry.effective_hierarchy_path(),
            "message": (
                f"Entry '{args.title}' added to knowledge store."
                if added
                else f"Entry '{args.title}' already exists (duplicate content — skipped)."
            ),
        }
        if warnings is not None:
            payload["warnings"] = warnings
        emit_json(payload)
        return EXIT_OK

    if action == "hook-context":
        return _run_hook_context(store, args)

    if action == "hook-session":
        return _run_hook_session(store, args)

    if action == "hook-stop":
        return _run_hook_stop(store, args)

    if action == "consolidate":
        from backend.knowledge import consolidate as consolidation

        chat_fn = None
        if not args.no_llm:
            try:
                chat_fn = consolidation.ollama_chat_fn(
                    workspace_path=str(getattr(args, "workspace", "") or ""), model=args.model,
                )
            except Exception as exc:
                sys.stderr.write(
                    f"note: no local model for merge proposals ({exc}); reporting groups without proposals\n"
                )
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
        if args.report_file:
            Path(args.report_file).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            payload["report_file"] = args.report_file
        emit_json(payload)
        return EXIT_OK

    if action == "delete":
        deleted = store.delete_entry(args.entry_id)
        emit_json({
            "deleted": deleted,
            "message": (
                f"Entry {args.entry_id} deleted."
                if deleted
                else f"Entry {args.entry_id} not found."
            ),
        })
        return EXIT_OK if deleted else EXIT_NOT_FOUND

    if action == "list-sources":
        repos = store.list_repos()
        emit_json({
            "total_entries": store.total_entries(),
            "sources": [_repo_to_dict(r) for r in repos],
        })
        return EXIT_OK

    if action == "stats":
        repos = store.list_repos()
        hierarchy = store.browse_hierarchy(depth=1)
        emit_json({
            "total_entries": store.total_entries(),
            "by_scope": store.scope_counts(),
            "source_count": len(repos),
            "top_level_category_count": len(hierarchy["nodes"]),
        })
        return EXIT_OK

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)
