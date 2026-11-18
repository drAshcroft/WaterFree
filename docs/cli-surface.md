# WaterFree CLI Surface

Replaces the MCP stdio servers with a single compiled executable that exposes
each former MCP tool as a `waterfree <area> <action>` subcommand. The goal:
agents call it via the shell, no MCP transport involved, and the executable
uses a launcher with a deployed Python backend and a shared Python environment.
Backend changes must be deployed to the installed runtime to affect commands
run outside the source checkout.

## Owned assets and prop lookup

The owned-asset catalog has its own `assetsearch.py` entry point; `props` and
`assets` are not WaterFree CLI areas. Use the `waterfree-assets` skill, which
resolves the configured library paths, or query this machine's catalog directly:

```powershell
& C:\Projects\.local\Scripts\python.exe C:\Projects\itch_assets\assetsearch.py "crate, godot" --subjects
& C:\Projects\.local\Scripts\python.exe C:\Projects\itch_assets\assetsearch.py "KayKit animation" --packs
```

This returns owned file or package records and their locations and rights;
it is not a gameplay-mechanic definition dictionary. Requests for a prop's
behavior require the relevant project's own definitions.

## Invocation shape

```
waterfree <area> <action> [--workspace <path>] [flags] [positional]
```

- **area** — one of: `todos`, `knowledge`, `index`, `testing`, `qa-summary`,
  `writing-grade`, `vision`, `imagegen`, `usage`
- **action** — area-specific verb (e.g. `list`, `add`, `search`, `delete`)
- **--workspace** — path to the project root. Defaults to CWD. Required for
  every `todos`, `index`, and `testing` action. Knowledge is global, but accepts
  `--workspace` as an ignored compatibility flag.
- **--json** — implied for tools that return data; CLI always prints JSON to
  stdout unless `--text` is passed for a specific tool.

`waterfree serve` (the persistent VS Code bridge) is unchanged. The legacy
`waterfree mcp <mode>` dispatch has been removed — agents call the CLI
subcommands directly.

## Conventions

| Concern        | Decision |
|----------------|----------|
| Output channel | Result goes to **stdout** as JSON. Progress/log lines go to **stderr** so callers can `2>/dev/null` them. |
| Exit codes     | `0` success. `2` validation/usage error. `3` not-found (task, entry, file). `4` external dependency missing (e.g. Ollama, repo not indexed). `1` internal error. |
| Multi-value flags | Use **repeated flags** for short lists (`--tag x --tag y`); use a single `--json '<inline>'` flag for nested objects (e.g. task `patch`, `targetCoord`). |
| Long-running ops | Stream human progress on stderr, emit final JSON on stdout when done. Add `--quiet` to suppress progress. |
| File I/O      | Path args are resolved relative to CWD, not `--workspace`. Use absolute paths in scripts. |
| Help           | `waterfree`, `waterfree todos`, `waterfree todos add` each print scoped help and exit 0. `-h` / `--help` accepted at any depth. |

## Area: todos

Mirrors `backend/mcp_todos.py`. Backed by `.waterfree/tasks.db` via
`backend.todo.store.TaskStore`.

| Action       | Flags / args                                                                       | MCP equivalent |
|--------------|------------------------------------------------------------------------------------|----------------|
| `list`       | `--status`, `--priority`, `--phase`, `--owner`, `--ready-only`, `--blocked`, `--blocked-on human\|external`, `--limit N`, `--offset N`, `--full`, `--fields F,...` | `list_tasks` |
| `search`     | `<query>` (positional), `--limit N`, `--phrase`, `--full`, `--fields F,...`        | `search_tasks` |
| `get`        | `<task-id\|key>`, `--full`, `--fields F,...`, `--notes-only`, `--tail N`          | - (single task lookup) |
| `get-next`   | `--owner NAME`, `--full`, `--fields F,...`                                         | `get_next_task` |
| `get-ready`  | `--limit N`, `--full`, `--fields F,...`                                            | `get_ready_tasks` |
| `schema`     | `--workspace`                                                                      | - (task schema) |
| `task-types` | `--workspace`                                                                      | - (enum discovery) |
| `validate`   | `--workspace`                                                                      | - (backlog validation) |
| `add`        | `--title T`, `--description D`, `--key`, `--priority`, `--phase`, `--owner-type`, `--target-file`, `--target-line`, `--json-file <path\|->`, `--full` | `add_task` |
| `update`     | `<task-id\|key>`, `--status`, `--priority`, `--phase`, `--key`, `--title`, `--description`, `--acceptance-criteria`, `--owner-type`, `--owner-name`, `--blocked-on human\|external`, `--blocked-reason`, `--unblock`, `--state`, `--state-file <path\|->`, `--append-ai-notes`, `--append-ai-notes-file <path\|->`, `--append-human-notes`, `--append-human-notes-file <path\|->`, `--ai-notes`, `--human-notes`, `--actual-minutes`, `--branch`, `--worktree`, `--patch '<json>'`, `--patch-file <path\|->`, `--full`, `--quiet` | `update_task` |
| `delete`     | `<task-id\|key>` or `--ids-file <path\|->` (`--ignore-missing`), `--dry-run`       | `delete_task` |
| `prune`      | `--status complete\|skipped` (repeatable), `--older-than AGE`, `--include-undated`, `--export FILE`, `--dry-run` | — (bulk delete of finished work) |
| `import`     | `--file <path\|->`, `--upsert`, `--dry-run`, `--full`                             | — (bulk `add_task`/`update_task`) |

All actions accept `--workspace` (default: CWD). Read/write actions emit
**compact** JSON (null/empty/default fields omitted) unless `--full` is passed.
`--fields` projects exactly the named top-level task fields (nulls included) on
`list`, `search`, `get`, `get-next` and `get-ready`.
`list`, `search`, and `get-ready` all return `{ "tasks": [...], "total": N }`
envelopes; `list` also includes `phases`. Wherever an action takes a task id,
the task's stable `key` is accepted too (case-insensitive): `get`, `update`,
`delete`. An unrecognized flag reports the failing subcommand's usage
(`usage: waterfree todos update ...`), exit `2` — this applies to every area.

**stdout only.** The todos area writes nothing informational to stderr
(PowerShell turns any native stderr into a NativeCommandError, so a note there
makes a successful call look failed). Advisories are JSON: `hint` on envelopes
and a `warnings` string list on task results (`add`, `update`, `get`). stderr
carries only errors, which always exit non-zero.

**Worktrees.** The todos area (only) maps a linked git worktree that has no
`.waterfree/tasks.db` of its own onto the main checkout's store (the parent of
`git rev-parse --git-common-dir`), so worktree agents need no `--workspace`.
This also applies to an explicit `--workspace` naming such a worktree; any other
explicit `--workspace` is used as given, and a worktree with its own store keeps
it. Redirected results carry `"workspace": "<main checkout>"` and
`"resolvedFrom": "worktree"`. When neither location has a store, read actions
exit `3` naming the main checkout; `add`/`import` create the store there.

`list` pages: `total` is always the unpaged count for the given filters, and
the envelope also carries `returned`, `offset`, `limit` and `truncated`. When
the page was cut it adds `nextOffset` and a `hint`, so a capped dump is never
mistaken for the whole backlog. `--blocked` lists unfinished tasks blocked by an
incomplete `blocks` dependency or a typed blocker; `--blocked-on` filters on the
typed blocker kind.

`search` matches **terms**: every whitespace-separated word must appear
somewhere in the task, in any field and any order. `_` and `-` are treated as
spaces on both sides (`mode lever` finds `mode_lever`). A double-quoted run
inside the query must be contiguous; `--phrase` makes the whole query one
contiguous phrase. A task whose `key` equals the query sorts first. Searched
fields: key, title, description, rationale, acceptanceCriteria, trigger, state,
aiNotes, humanNotes, targetCoord file/class/method, owner name, phase. The
default row is a summary (`id`, `key`, `title`, `status`, `priority`, `phase`,
`owner`, plus a `match` snippet naming the field that hit); `--full` emits whole
tasks. The envelope adds `returned`, `mode`, `terms`, and a `hint` whenever
nothing matched ("0 tasks contain all N terms; M contain at least one") or the
limit truncated the result.

On `update`, discrete flags cover the
common edits without JSON; `--patch` is for fields without a flag and discrete
flags win on conflict. `--append-ai-notes` / `--append-human-notes` add a
paragraph to the existing notes (the `*-file` variants read the paragraph from a
UTF-8 file or `-` for stdin); `--ai-notes` / `--human-notes` replace them and add
a `warnings` entry when the replacement is less than half the length of what it
discarded. The replace and append flags for one field are mutually exclusive,
and at most one input per call may read stdin. `add --json-file` reads one complete task object, while
`update --patch-file` reads one JSON patch object; both accept `-` for stdin.

Lifecycle dates are stamped on status changes made through `update` and
`import --upsert`: moving to `complete` sets `completedAt`, reopening clears it,
and the first move to `executing` sets `startedAt`; a date set explicitly in the
patch (and different from the stored one) wins. Tasks created already complete
are not back-dated.

`state` is an overwritable markdown field for the current verdict, open items
and rejected approaches (soft cap ~1,500 characters; `aiNotes` stays the
append-only history). `update --state TEXT` / `--state-file <path|->` replace it
wholesale (`--state ''` clears it). It is printed before the notes, searched, and
projectable. `get --notes-only` emits id/key/title/status, `state`, `aiNotes` and
`humanNotes`; `--tail N` (implies `--notes-only`) keeps the last N paragraphs of
each notes field and reports `truncated`. `get` adds a `warnings` entry when
`aiNotes` exceeds ~5,000 characters, and `update` warns when `state` exceeds its
soft cap.

`blockedOn` (`human` | `external` | null) is a typed blocker for work waiting on
a person or something outside the project. `update --blocked-on KIND
--blocked-reason TEXT` sets it (a reason is required unless one is already
stored); `--unblock` clears both fields. A task with `blockedOn` keeps its status
but is excluded from `get-next`, `get-ready` and `list --ready-only`.

`workRef` (`{branch, worktreePath, headCommit, recordedAt}`) records where a
task's work lives. `update` sets it automatically when the task moves to
`executing` from inside a checkout of the store's repository, and refreshes it
when an executing task is updated (or completed) from a linked worktree, unless
it already names a different worktree that still exists. `--branch` /
`--worktree` set it explicitly; `--patch '{"workRef": null}'` clears it. When a
task has a `workRef`, `get` adds `workStatus`: live `branch`, `headCommit`,
`dirtyFiles`, `ahead`/`behind` against `main` (or `master`), `pathExists`, and
the newest file under `artifacts/<KEY>/` as `receipt`. A deleted worktree
degrades to the stored values (`pathExists: false`, plus `branchExists` and
ahead/behind from the main repository when the branch survives).

`delete --ids-file` reads ids/keys (one per line, `#` comments allowed, or a JSON
array / `{"tasks": [...]}` such as `list --fields id` output) and deletes them in
one transaction. If any entry matches no task nothing is deleted and the exit
code is `3` (`notFound` lists them) unless `--ignore-missing`. Output:
`{deletedCount, matched, tasks, notFound, dryRun}`; remaining tasks whose
`dependsOn` named a deleted task are reported as `orphanedDependents` (single
deletes report them too). `prune` deletes finished tasks (`--status`, default
`complete`; only `complete`/`skipped` are accepted) after writing them to
`--export FILE` — required unless `--dry-run`, merged into an existing export,
replaced atomically, and nothing is deleted if the export fails (exit `1`).
`--older-than 30d|12h|2w` measures from `completedAt`; tasks without one are
reported as `skippedUndated` unless `--include-undated`. Dependency edges from
remaining tasks to pruned tasks are removed (`detachedDependencies`) so those
tasks stay ready. The export is a valid `import --file` input.
All JSON file and stdin inputs use UTF-8 (with an optional UTF-8 BOM); malformed
UTF-8 and invalid Unicode return a usage error before any task is persisted.
When combined, file data is applied first, then inline JSON and discrete flags
override it. File paths are resolved relative to CWD.
`schema` prints the complete task JSON schema, including valid enum values.
`task-types` prints the accepted `taskType` values. Invalid enum values reported
by task writes include the accepted values inline.
`validate` checks the persisted backlog for missing required fields, duplicate
keys, unresolved dependencies and dependency cycles (errors), plus warnings for
title conventions, tasks that carry a `blockedReason` with neither an incomplete
dependency nor a `blockedOn` (`ready_with_blocked_reason`), and `blockedOn`
without a reason (`blocked_on_without_reason`). It returns
`{ "ok": bool, "issueCount": N, "errorCount": N, "warningCount": N, "issues": [...] }`
and exits `2` when errors are present.

Tasks have an optional stable `key` (e.g. `GOV-001`), settable via `add --key`
or `update --key`. It must be unique across the workspace
(`add`/`update` reject a collision with exit code 2). Entries in `dependsOn`
accept `{"key": "GOV-001", "type": "blocks"}` as an alternative to
`{"taskId": "<uuid>", "type": "blocks"}` — resolved to the real id at write
time, so tasks can reference each other by a name you chose instead of a
generated UUID. A key written into `taskId` (or a bare string entry) is resolved
the same way, including keys created earlier in the same `import` file. On the
CLI, `add`/`update` reject a `dependsOn` entry that names no task.
`search`/`list`/`get-*` include `key` in their output when set.

`import` reads a JSON file (`--file backlog.json`, or `--file -` for stdin —
same convention as `knowledge add --code -`) containing either a bare array of
task objects or `{"tasks": [...]}` (so `todos list --full` output round-trips
straight back into `import`). Items are matched to existing tasks by `key`:
an unseen key is created, a key that already exists is only updated when
`--upsert` is passed (otherwise it's a validation error), and an item with no
`key` is always created. The whole batch is validated up front — duplicate
keys within the file, unresolved `dependsOn` references, and self-dependencies
are all reported as `errors` — and nothing is written if any item fails,
whether or not `--dry-run` was passed. Exit code is `2` when `errors` is
non-empty.

## Area: knowledge

Mirrors `backend/mcp_knowledge.py`. Backed by the global store at
`~/.waterfree/global/knowledge.db`; `--workspace` is accepted for agent
muscle-memory compatibility but does not change the global store location.

| Action          | Flags / args |
|-----------------|--------------|
| `search`        | `<query>`, `--limit N`, `--repo NAME`, `--path P`, `--scope default\|all\|global\|project\|assets`, `--workspace` (its basename is the preferred repo), `--full` or `--brief` |
| `browse`        | `[PATH]` or `--path P`, `--depth N`, `--include-entries` / `--list`, `--entry-limit N`, `--workspace`, `--full` |
| `get`           | `<entry-id>` (full id, unique id prefix of 6+ chars, or exact title), `--text`, `--workspace` |
| `history`       | `<entry-id>` (also a deleted entry's id/prefix), `--field F` (repeatable), `--limit N` (50), `--full`, `--workspace` |
| `add`           | `--title`, `--description`, `--code-file PATH` (or `--code -` for stdin; both optional), `--snippet-type` (required with code, defaults to `convention` without), `--source-repo`, `--source-file`, `--tag T` (repeatable), `--context` or `--context-file PATH\|-`, `--source-repo-url`, `--hierarchy-path`, `--scope global\|project\|assets`, `--no-check`, `--force`, `--dry-run`, `--quiet`, `--workspace` |
| `update`        | `<entry-id>` (id, unique prefix or exact title), any of `--title`, `--description` / `--description-file` / `--append-description`, `--code` / `--code-file`, `--snippet-type`, `--tag T` (repeatable; replaces the list), `--context` / `--context-file` / `--append-context`, `--source-repo`, `--source-file`, `--source-repo-url`, `--hierarchy-path`, `--scope`; or `--restore-revision N` [`--restore-field F`]; `--quiet`, `--workspace` |
| `hook-context`  | reads a Claude Code `UserPromptSubmit` event on stdin; `--max-rows N` (3), `--min-terms N` (2), `--max-terms N` (12), `--workspace`. Prints `hookSpecificOutput.additionalContext` only when an entry contains every key term (identifiers first) of the prompt; otherwise prints nothing. Fragment: `skills/waterfree-knowledge/hooks/claude-settings.hooks.json`, installed with `install_claude.ps1 -InstallHooks`. |
| `hook-session`  | reads a `SessionStart` event on stdin; `--max-rows N` (3), `--workspace`. Prints one orientation line (shared-lesson count, this project's count, its most-retrieved entries); nothing for an empty store. |
| `hook-stop`     | reads a `Stop` event on stdin; `--workspace`. When `last_assistant_message` carries a lesson marker (several attempts, workaround, root cause, turned out, gotcha, did not work) and `stop_hook_active` is false, prints `decision: block` with a one-paragraph reason asking for the insight to be filed; otherwise nothing. |
| `consolidate`   | `--window N` (days of retrieval history, 90), `--min-age N` (30), `--threshold F` (0.5), `--max-groups N` (25), `--max-group-size N` (5), `--include-assets`, `--no-llm`, `--model M`, `--apply`, `--report-file PATH`, `--workspace` |
| `delete`        | `<entry-id>` (full id or unique prefix; not a title), `--workspace` |
| `list-sources`  | `--workspace`, `--full` |
| `stats`         | `--workspace`, `--full` |

Note: `--code-file` is the preferred way to pass a snippet body; shell-escaping
multi-line code through argv is painful. `--code -` reads from stdin. Code is
optional: a lesson, decision or convention with nothing to paste is stored with
an empty `code` and is deduplicated on title + description + context instead of
on the code hash. `knowledge search` returns `{ "entries": [...], "total": N, "preferred_repo": ..., "scope": ..., "mode": "summary"|"brief"|"full", "hint"?, "suggestions"? }`.
Ranking is precision-first: entries containing every query word come first
(BM25 order within the tier), then any-word matches re-ranked by how many
distinct query words their title/description/tags contain (code only breaks
ties). Entries whose `source_repo` basename equals the `--workspace` basename
sort first within each tier, and the preferred repo's all-word matches are
fetched separately before the tier is cut to `--limit`, so they are never
dropped in favour of other repos'; `--repo` restricts to one repo (applied in
SQL before the row limit). Default rows omit `code` and `context` (they carry
`code_chars`/`context_chars`, and `source_note` when present); `--full` restores
them and `get <id>` returns one entry. `--brief` rows are exactly `id`, `title`,
`hierarchy_path`, `source_repo`, printed one minified row per line (~2.3 KB for
10 real rows against ~9 KB default). A `--path` that matches no node adds the
nearest paths to the hint.
Every entry carries a `scope`: `global` (default), `project` (visible only to searches
whose `--workspace`/cwd basename matches its `source_repo`) or `assets` (automatic for
`assets/*` hierarchy paths). `search --scope` defaults to global + own-project; `all`
or a single scope override it. Existing databases are backfilled once on open:
`assets/*` rows become `assets`, entries whose title starts with their own repo name
become `project`, the rest stay `global`. `stats` reports `by_scope`.
`add` runs a quality gate unless `--no-check`: `warnings` lists a
`near_duplicate` (an entry already contains every word of the title; `ids`,
`titles`, `shared_terms`, and `recent_id` when the twin was filed from the same
repo in the last 15 minutes), `no_hierarchy`, and `reads_like_a_trace`
(first-person narration; `evidence` quotes the sentences; numbered lists alone
do not count). Only `near_duplicate` blocks: exit 2, nothing saved, unless
`--force` or the body is an exact duplicate. The add JSON starts with `added`,
`id`, `saved`, `summary`, `revision`; an exact duplicate returns `added: false`,
`saved: true` and the existing `id`/`revision` (`duplicate_of`); a blocked add
returns `id: null`, `blocked: true`, `duplicate_of`. Advice goes into `notes`
(`saved anyway as <id>; revise with knowledge update <id>`, path normalisation,
an uncommitted `--source-file`); like todos, the knowledge area writes nothing
informational to stderr unless stderr is an interactive terminal, which gets a
readable copy. `--quiet` prints only `{id, added, revision}` plus `warnings`
(the kinds) when the gate fired (blocked: `{id: null, added: false, blocked,
duplicate_of}`). With `--source-file` inside a git checkout, `add`/`update` record
`source_ref` `{repo, worktreePath, branch, commit, path, status, committed}` (one
`git status` call; nothing outside git); rows then carry `source_note`
(`uncommitted in worktree X ...`, or `<file> is not in this workspace` when the
reader's checkout of the same repo lacks it).
`get` returns one entry (exit 3 when unknown). `get`/`update`/`delete`/`history`
accept a unique id prefix of 6+ hex characters; an ambiguous prefix exits 2 and
lists `id  title` candidates on stderr, a shorter one exits 2. `update` revises
an entry in place: the id is kept, `revision` is incremented, `updated_at` is
stamped, the full-text index is refreshed, and only the passed fields change; it
exits 2 when no field was given or when the revised content would collide with
another entry. `--append-description`/`--append-context` add a paragraph; a
replace that discards more than half of the old text (by words) adds a `warnings`
string to the update result (`--quiet`: a `warnings` count).
Every changed field's previous value goes to the `knowledge_entry_history` table
(`delete` snapshots all fields first); `history <id>` lists rows `{revision,
field, change, changed_at, old_chars, old_value}` newest first (values over 400
chars clipped unless `--full`), and `update <id> --restore-revision N` puts the
revision-N values back as a new revision. `update --quiet` prints only `{id,
updated, revision}`. `browse` takes the path positionally or as `--path`; a path
with no node returns `hint` + `suggestions` (nearest paths ignoring case,
spaces and separators, then trailing segments and edit distance), a node with a
separator twin returns `see_also`, and `--include-entries`/`--list` rows use the
search-row shape (`--full` for code/context).
`consolidate` is the store's maintenance pass: it groups near-duplicate entries
(title vocabulary overlap, or title+description overlap within one hierarchy),
asks the local model on the `knowledge_consolidate` stage for one merged entry per
group, finds relative-date phrases and rewrites them against the entry's creation
date, lists entries never retrieved within the window (from the usage log) and
entries with no hierarchy path. It only reports unless `--apply`, which updates the
oldest id of each mergeable group, deletes the others, and applies date rewrites;
stale entries are never deleted. Oversized groups and asset rows are excluded.

`trace`, `detect-changes` and `architecture` fit their output to `--budget-tokens`
(approximate, 4 chars per token): rows are dropped least-important-first (edges
before nodes, module graph before god nodes, lowest degree first) and a `budget`
block reports `estimated_tokens`, `dropped` per list, `truncated` and a hint.

## Area: usage

The CLI's own usage log. Every dispatch of any other area appends one JSON
record to `~/.waterfree/global/usage.jsonl` (override with
`WATERFREE_USAGE_LOG=<path>`, disable with `WATERFREE_USAGE_LOG=0`): `ts`,
`source` (`cli` | `transcript`), `area`, `action`, `workspace`, clipped `argv`,
`query`, `exit_code`, `duration_ms`, `result_bytes`, and when the action emitted
an envelope its `hits` (`total`), `returned`, `truncated`, `hint` and the first
ten `hit_ids`; plus `agent` (`AI_AGENT` env) and `session`
(`CLAUDE_CODE_SESSION_ID`). Logging is fail-silent and never alters a command's
output or exit code. `usage` actions are not logged.

Permission failures in optional logging are quiet by default. Set
`WATERFREE_USAGE_LOG_DEBUG=1` to see skipped-write diagnostics, or point
`WATERFREE_USAGE_LOG` at a writable file inside the active workspace.

| Action               | Flags / args |
|----------------------|--------------|
| `summary`            | `--since 30d\|7d\|12h\|2w\|all`, `--area A`, `--workspace P`, `--source cli\|transcript`, `--top N` |
| `tail`               | `-n N`, `--area A`, `--workspace P` |
| `path`               | — |
| `import-transcripts` | `--claude-projects DIR` (default `~/.claude/projects`) |

`summary` aggregates per action: calls, sessions, workspaces, error rate,
median/p90/total result bytes, zero-hit rate and median hits for search-like
actions; plus `by_area`, `by_agent`, `by_workspace`, `by_day`, a `knowledge`
block (searches, adds, adds_per_search, distinct entries ever retrieved, top
retrieved) and top / top-empty queries. `import-transcripts` pairs every
`waterfree` shell call in Claude Code's local transcripts with its tool result
and rewrites `usage-transcripts.jsonl` wholesale (idempotent), so history from
before the log existed is included. See docs/20_HARNESS_HELPERS_RESEARCH.md.

## Area: index

Mirrors `backend/mcp_index.py`. Backed by `GraphClient` against the workspace
graph DB.

| Action               | Flags / args |
|----------------------|--------------|
| `build`              | (alias: `index`) — full index |
| `status`             | — |
| `search-code`        | `<query>`, `--max N`, `--include-worktrees` (nested git worktrees are skipped by default) |
| `search-graph`       | `<query>`, `--node-type T`, `--limit N` |
| `get-snippet`        | `<qualified-name>`, `--scope procedure|neighbors|class` |
| `trace`              | `<function>`, `--direction callers|callees|both`, `--depth N`, `--budget-tokens N` (1500; 0 = unlimited) |
| `detect-changes`     | `--scope all|<files>`, `--depth N`, `--budget-tokens N` (1500) |
| `architecture`       | `--aspect a,b,c` (default `languages,layers,god_nodes`), `--all-aspects`, `--budget-tokens N` (2500) |
| `list-projects`      | — (no `--workspace`) |

All actions except `list-projects` accept `--workspace`.

## Area: testing

Mirrors `backend/mcp_testing.py`. Auto-detects godot → pytest → jest → vitest →
unittest → npm-scripts → dotnet; `playwright` and `godot-script` are opt-in only.

| Action       | Flags / args |
|--------------|--------------|
| `run`        | `--workspace`, `--full`, `--summary`, runner flags (below) |
| `run-one`    | `<name-substring>`, `--workspace`, `--full`, `--summary`, runner flags |
| `list`       | `--workspace`, `--full`, runner flags |
| `logs`       | — |
| `summarize`  | `--workspace` |
| `await`      | `--log PATH`, `--until REGEX`, `--timeout DURATION` (30m), `--pid N`, `--summary-pattern REGEX`, `--max-lines N` (60), `--fresh` \| `--include-existing` |

Runner flags: `--runner {dotnet,godot,godot-script,jest,npm-scripts,playwright,pytest,unittest,vitest}`,
`--godot-path`, `--budget DURATION`, `--stall DURATION`, and for `godot-script`
only: `--script res://...` (implies `--runner godot-script`), `--lane NAME`
(repeatable), `--arg=VALUE` (repeatable), `--junit-xml PATH`. Durations: `90`,
`90s`, `5m`, `1h30m`, `250ms`.

Exit codes for `run`/`run-one`: `0` green, `1` a test failed, `2` usage, `4`
the tests did not produce a verdict — no framework, toolchain missing, zero
tests discovered, **run-one matched nothing**, or the run was **incomplete**
(`--budget`/`--stall` kill, godot-script with no lane summary).

Result JSON: `runner`, `passed`, `failed`, `skipped`, `total`, `results[]`,
`elapsedSeconds`; plus `incomplete: true`, `reason` (`budget` | `stall` |
`timeout` | `no_summary`), `limitSeconds`, `error`, `firstError` /
`firstErrorLocation` (first Godot `SCRIPT ERROR`/`Parse Error` and its
`file:line`) when the run has no verdict; `receipt` for `godot-script`
(native `exitCode`, `command`, git `fingerprint` {`head`, `dirty`},
`lanes[]`, `verdict`, `junit`, `receiptFile` holding full stdout/stderr).

A zero-match `run-one` prints a JSON error on stdout (exit 4):
`{error, code: "no_tests_matched", runner, filter, available, skipped}`; when
the runner was auto-detected it adds `autoDetected`, `otherRunners[]`
(`runner`, `evidence`, `matches` where the runner can list statically,
`retry`, `optIn`) and a `hint`.

`await` prints `{matched, reason: matched|pid_exited|timeout, elapsedSeconds,
log, matchLine, summaryLines[], summaryLineCount, bytesRead, logExists}`; exit
0 matched, 4 otherwise. Only content written after the call started counts.

`--summary` adds a `summary` key to the JSON: an LLM root-cause grouping of the
failures, produced by `backend/testing/summary.py`. It is advisory — if the
model is unreachable the run still reports its real result, with the reason in
`summaryError`, and the exit code is unchanged. `summarize` does the same for
the stored output of the last run, without re-running anything.

Both route through the `testing` stage in `.waterfree/providers.json`.

## Area: qa-summary

Implemented in `backend/qa_summary/core.py`. Runs on local Ollama by default;
route the `qa_summary` stage in `.waterfree/providers.json` to an `openrouter`
provider to run it remotely instead (key from `$OPENROUTER_API_KEY`, since the
CLI cannot read VS Code SecretStorage).

| Action       | Flags / args |
|--------------|--------------|
| `ask`        | `<file-or-url>`, `--question Q` (or `-q`), `--workspace PATH` |

## Area: writing-grade

Implemented in `backend/writing_grade/core.py`. It grades one local creative-
writing file on six genre-aware dimensions, each scored from 0 through 20 with
exactly one feedback sentence. The CLI computes the total and percentage rather
than trusting the model's arithmetic. Short files are graded directly; long
files use ordered map/reduce evidence.

| Action       | Flags / args |
|--------------|--------------|
| `grade`      | `<file>`, `--workspace PATH` |

The success response is always a JSON object containing `genre`, six entries in
`dimensions`, `overall` (score out of 120 and percentage), routing metadata,
source size, and chunk count. It routes through the `creative_writing` stage.

## Reader-stage model selection

`qa_summary`, `tutorial`, `testing`, and `creative_writing` are the *reader*
stages. A provider must
name one in `routing.useForStages` to claim it; otherwise readers stay on local
Ollama.

For an `openrouter` provider, a reader stage's model may be a sentinel instead
of a concrete id:

| Model id     | Meaning |
|--------------|---------|
| `auto:free`  | Zero-priced models, widest context window first, with a short cheapest-paid tail |
| `auto:floor` | Cheapest priced models first — the price floor |

Both are resolved at run time against OpenRouter's live `/api/v1/models`, cached
for a day in `.waterfree/openrouter-models.json` (a stale cache is served if the
refresh fails, so an offline run still works). Each expands to an ordered
*chain* of candidates ending at local Ollama: a rate-limited free endpoint falls
through to the next candidate per request rather than failing the run. These are
the defaults for the reader stages on OpenRouter — set a concrete model id
to opt out. See `backend/llm/openrouter_catalog.py`.

Exit code `4` if the resolved provider is unavailable — Ollama not running, the
model not installed, or no API key for a remote provider.

## Area: vision

Implemented in `backend/vision/`. Runs a local vision model through the Ollama
daemon. `look` accepts image files; `web` captures a URL with Playwright and
retains both viewport and full-page screenshots before local analysis.

| Action      | Flags / args |
|-------------|--------------|
| `look`      | `<image>...` (up to 4; exactly 1 for classify), `--purpose`, `-q/--question`, `--model`, `--tier`, `--timeout SECONDS`, `--labels A,B,C`, `--workspace` |
| `web`       | `<url>`, `--purpose`, `--question`, `--model`, `--tier`, `--timeout SECONDS`, `--labels A,B,C`, `--mobile`, `--ready-selector`, `--wait-ms`, `--timeout-ms`, `--full-page`, `--capture-only`, `--output-dir`, `--workspace` |
| `models`    | `--workspace` |
| `purposes`  | `--workspace` |
| `pull`      | `--tier {small,large}` or `--model <id>`, `--workspace` |

Purposes select both the model tier and the framing of the question:
`describe` (small), and `triage` / `ui` / `function` / `text` / `compare` /
`classify` (large). A custom `-q` question lifts `describe` to the large tier
(the small tier agrees with candidates a question names); `--tier` / `--model`
override. Results carry `tier`, `tierReason`, `advisory` and a `warnings` list
of `{code, message}` — e.g. `leading-question` when `-q` names candidate answers
(parenthesised lists, "such as", "e.g.", "X or Y or Z", "is there a ...").

`classify` requires `--labels` (2–20, comma-separated; missing is exit `2`),
always adds `none` and `uncertain`, shuffles the order per call (recorded as
`labelOrder`) and returns `{label, evidence, confidence: low|medium|high}`. An
out-of-set label gets one stricter retry, then `label: "uncertain"` with the raw
reply in `answer`.

Two tiers: `small` = moondream (~1.7 GB), `large` = qwen2.5vl:7b (~6 GB).
Override per-invocation with `--model`, or globally with
`$WATERFREE_VISION_MODEL_SMALL` / `$WATERFREE_VISION_MODEL_LARGE`.

**Nothing is downloaded implicitly.** A missing model exits `4` with the exact
`waterfree vision pull` command — these are gigabytes, and a describe should
never silently cost 6 GB of disk.

**Busy host.** `--timeout` (default 300 s, `$WATERFREE_VISION_TIMEOUT`) bounds
each model attempt (`web --timeout-ms` is page loading only). Timeouts and
Ollama 503s are retried with backoff (`$WATERFREE_VISION_RETRIES`, default 1,
max 3); when all fail, exit `4` with "Ollama did not answer in N s ...; the
host may be under load" naming `--timeout`. The pre-flight model listing waits
at most `$WATERFREE_VISION_LIST_TIMEOUT` (10 s) and, if Ollama is too busy,
proceeds with a `model-check-skipped` warning; `--model` skips it. `models`
reports `installed: null` when Ollama cannot be asked.

Images are downscaled to 1280px on the long edge (`$WATERFREE_VISION_MAX_EDGE`)
and re-encoded as PNG before being sent.

## Area: imagegen

Implemented in `backend/imagegen/`. Diffusers generation requires `diffusers`,
`accelerate`, `sentencepiece`, `protobuf` and CUDA-capable PyTorch. The ComfyUI
HTTP backend uses an existing server and its registered checkpoints/LoRAs.

| Action    | Flags / args |
|-----------|--------------|
| `make`    | `<prompt>`, `-n/--count`, `--preset`, `--steps`, `--guidance`, `--width`, `--height`, `--negative-prompt`, `--seed`, `--offload`, `--output-dir`, `--hf-token`, `--backend {diffusers,comfy}`, `--image`, `--mask`, `--strength`, repeatable `--lora PATH=WEIGHT`, `--comfy-url`, `--checkpoint`, `--timeout`, `--workspace` |
| `models`  | `--workspace` |
| `status`  | `--workspace`, `--backend`, `--comfy-url` |
| `init`    | `--workspace` |
| `unload`  | `--workspace` |

Presets: `sd35-medium` (default, ~11 GB), `sdxl` (~7 GB), `sdxl-turbo` (~7 GB),
`flux-schnell` (~24 GB, needs sequential offload on a 12 GB card). Settings
resolve in three layers, later winning: preset defaults → `.waterfree/imagegen.json`
→ CLI flags.

Weights download to the Hugging Face cache on first use, not the workspace.
SD3.5 and FLUX are gated repos: accept the licence and set `$HF_TOKEN`.

Images and a reproducibility sidecar `.json` are written to
`<workspace>/.waterfree/generated`. A batch shares one generator, so only the
first image records its seed.

Masks use white for edits and black to preserve pixels. Sidecars include input
hashes, editing mode and LoRA weights. Diffusers load progress on stderr reports
observed cache-byte growth with unknown totals left null. ComfyUI job timeouts
report the job ID without interrupting another user's work.

## Out of scope (deliberately)

- **No `debug` area.** The `waterfree-debug` skill is being removed (T8); its
  MCP server (`backend/mcp_debug.py`) goes with it.
- **No daemon mode.** Each CLI invocation is a short-lived process. The VS Code
  extension keeps using `waterfree serve` for the persistent bridge.
- **No shell completion (yet).** Possible follow-up.

## JSON contract notes

- All structured output is `json.dumps(obj, indent=2)` for human readability.
  Callers should parse with `json.loads` — don't grep.
- Errors going to stderr are plain text, prefixed with `error:`. Errors that
  happen mid-JSON-serialization are emitted on stdout as
  `{"error": "<message>", "code": "<short_code>"}` and accompanied by a
  non-zero exit code.
- Schemas are intentionally identical to the MCP tool output so SKILL.md
  rewrites (T7) only need to change the invocation mechanism, not parsing.
