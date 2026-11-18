---
name: waterfree-todos
description: Use the `waterfree todos` CLI to find the next ready task, record new work, and update task status as you implement.
---

# WaterFree — Task / Todo Store

Workspace backlog in `.waterfree/tasks.db`. Run each command in whatever shell
you have — Bash or PowerShell. `waterfree` is on PATH, so the command text is
identical in both; every example below is a single line so nothing depends on
shell-specific line continuations. Every command prints JSON to stdout (parse
with `json.loads`). Add `--workspace <path>` to target a project other than the CWD.

**Nothing informational goes to stderr.** Advisories travel in the JSON: a
`hint` string on envelopes (`list`, `search`, `delete`, `prune`) and a `warnings`
list on task results. stderr carries only real errors, which always exit
non-zero — so a PowerShell wrapper never sees a NativeCommandError on success.

Output is **compact** by default — null/empty/default fields are omitted (no
`owner` ⇒ unassigned, no `timing` ⇒ one_time, no `taskType` ⇒ impl). Add `--full`
only when you specifically need the raw shape. `list`, `search`, and `get-ready`
return a consistent envelope: `{ "tasks": [...], "total": N, ... }`.

Anywhere a command takes a task id, the task's stable `key` (e.g. `GOV-001`,
case-insensitive) works too: `get`, `update`, `delete`.

## Working from a git worktree

From a linked worktree (e.g. `.claude/worktrees/agent-*`) that has no
`.waterfree/tasks.db` of its own, every `todos` command uses the **main
checkout's** store automatically — no `--workspace` needed. Results then carry
`"resolvedFrom": "worktree"` and `"workspace": "<main checkout>"` so the
redirect is visible. A worktree that has its own store keeps using it, and an
explicit `--workspace` still wins. If neither the worktree nor the main
checkout has a store, read commands fail with a message naming the main
checkout (`add`/`import` create the store there).

## Reading — start here

```bash
waterfree todos get-next                 # the one task to work on now (or null)
waterfree todos get-ready --limit 5      # the next few unblocked tasks, by priority
waterfree todos search "auth rate limit" # find tasks containing all of those words
waterfree todos get GOV-001              # one task, by key or id
waterfree todos get GOV-001 --notes-only --tail 3   # state + the last 3 note paragraphs
waterfree todos validate                 # check backlog consistency
```

`get-next` returns the highest-priority unblocked task. **Call it before starting
work** so you don't duplicate effort. Reach for `get-ready`/`search` when you need
more than one candidate, and `get <id|key>` to resolve a `dependsOn.taskId` or a
key to its task.

**Read `state` first.** A task's `state` is its current verdict, open items and
rejected approaches; `get` prints it before the notes. When `aiNotes` passes
~5,000 characters `get` adds a `warnings` entry — refresh `state` rather than
reading the whole log. `get --notes-only` returns just id/key/title/status,
`state`, `aiNotes` and `humanNotes`; `--tail N` (implies `--notes-only`) keeps
the last N paragraphs of each notes field and reports `truncated`.

`--fields key,title,priority,state` projects exactly those top-level fields on
`list`, `search`, `get`, `get-next` and `get-ready` (paging metadata remains).

When the task has a `workRef` (see below), `get` adds `workStatus` with the live
git state of that checkout: `branch`, `headCommit`, `dirtyFiles`, `ahead`/`behind`
against `main` (or `master`), and the newest `artifacts/<KEY>/` file as
`receipt`. If the worktree was deleted, `workStatus.pathExists` is false and the
recorded branch is checked in the main repository instead (`branchExists`).

Run `validate` when task output looks inconsistent; it reports missing fields,
duplicate keys, unresolved dependencies and cycles as errors, and as warnings:
a `blockedReason` with no blocker behind it, and `blockedOn` with no reason.

### How `search` matches

- Every word must appear somewhere in the task (any field incl. `state`, **any order**):
  `search "DIQ difficulty evaluator"` finds "DIQ: the difficulty evaluator ...".
- `_` and `-` count as spaces on both sides: `search "mode lever"` finds `mode_lever`.
- Quote a run of words to require them contiguous: `search '"human rating" loop'`.
  `--phrase` treats the whole query as one contiguous phrase.
- A task whose key equals the query is returned first.
- Rows are summaries: `id`, `key`, `title`, `status`, `priority`, `phase`, `owner`
  and a `match` snippet naming the field that hit. Pass `--full` for whole tasks.
- The envelope reports `total` (all matches), `returned`, `mode`, `terms`, and a
  `hint` when nothing matched or `--limit` cut the results — so an empty result is
  never mistaken for "no such task". Check for duplicates with `search` before `add`.

> **Avoid `waterfree todos list`.** It dumps up to 50 tasks and burns tokens fast.
> Use `get-next` / `get-ready` / `search` instead. Only fall back to `list` (with a
> tight `--status`/`--priority`/`--blocked`/`--limit` filter and `--fields`) when
> you truly need a survey. `list` always reports the true `total`; when the page
> is cut it sets `"truncated": true`, `nextOffset` and a `hint`.

## Updating a task — no JSON needed

Explicit `--workspace` paths must name an existing directory. On Windows, use
quoted paths or forward slashes (`C:/Projects/name`); drive-relative paths such
as `C:Projectsname` are rejected instead of creating a stray task database.
JSON `null` clears `blockedReason`, `blockedOn`, `state`, `workRef` and `phase`
on update and import/upsert.

Set stable identifiers with `todos add --key KEY` (or `update --key KEY`); keys
are separate from titles. Record any prerequisite new mechanic explicitly in
rationale and acceptance criteria.

Use discrete flags for the common writes — they sidestep shell-quoting problems:

```bash
waterfree todos update <id> --status executing         # start (stamps startedAt, records workRef)
waterfree todos update <id> --status complete          # close (stamps completedAt)
waterfree todos update <id> --priority P1 --phase v2
waterfree todos update <id> --append-ai-notes "2026-09-25: blocked on missing migration"
waterfree todos update <id> --append-ai-notes-file notes.md   # long/quoted text: UTF-8 file or '-' for stdin
waterfree todos update <id> --state-file state.md      # replace the current state
waterfree todos update <id> --acceptance-criteria "All importer tests pass"
waterfree todos update <id> --owner-type agent --owner-name claude
```

Flags: `--status` `--priority` `--phase` `--key` `--title` `--description`
`--acceptance-criteria` `--owner-type` `--owner-name` `--blocked-on`
`--blocked-reason` `--unblock` `--state` `--state-file` `--append-ai-notes`
`--append-ai-notes-file` `--append-human-notes` `--append-human-notes-file`
`--ai-notes` `--human-notes` `--actual-minutes` `--branch` `--worktree`.
Only one `*-file` flag can read stdin (`-`) per call. An unknown flag prints
`usage: waterfree todos update ...`. Statuses:
`pending | executing | complete | skipped` (plus `annotating | negotiating` for
the annotation flow). Priorities: `P0 | P1 | P2 | P3 | spike` (P0 highest).

**Dates are automatic:** moving to `complete` stamps `completedAt`, reopening
clears it, and the first move to `executing` stamps `startedAt`. A date you set
yourself in `--patch` wins.

**Notes:** `--append-ai-notes` / `--append-human-notes` (and the `*-file`
variants) add a paragraph and keep what is already there — use these for dated
breadcrumbs. `--ai-notes` / `--human-notes` **replace** the whole field; the
result carries a `warnings` entry when that throws away more than half of the
earlier text.

For a field without a flag, use `--patch '<json>'` (discrete flags win on conflict).
In `dependsOn`, use `{"key": "GOV-001"}` or `{"taskId": "<id or key>"}` — keys are
resolved to ids, and an unknown reference is rejected.
Recurring tasks (`timing: recurring`) auto-reset to `pending` when set `complete`.

### Stopping protocol — state vs. notes

When you stop (done, blocked, or out of time):

1. **Overwrite `state`** (`--state-file -` or `--state`) with the current picture,
   under these headings, in ~1,500 characters or less:
   `## verdict` (where it stands), `## open` (what is left), `## rejected`
   (approaches tried and abandoned, and why — so nobody re-tries them).
2. **Append dated detail to `aiNotes`** (`--append-ai-notes-file -`): what you ran,
   what you saw. `aiNotes` is the history; `state` is what the next agent reads.
3. Set the status (`complete` / back to `pending`) or a blocker (below).

### Blocked on a person or something outside the project

Do **not** create a stand-in "decide X" task to depend on, and do not push the
task back to plain `pending`. Mark the blocker:

```bash
waterfree todos update <id> --blocked-on human --blocked-reason "Owner must choose the village rules"
waterfree todos update <id> --blocked-on external --blocked-reason "Waiting for the vendor SDK 2.1 release"
waterfree todos list --blocked --fields key,title,blockedOn,blockedReason   # everything waiting
waterfree todos update <id> --unblock                                       # back to the ready queue
```

A task with `blockedOn` keeps its status but is skipped by `get-next`,
`get-ready` and `list --ready-only`. `--blocked-on` needs a reason (or an
existing `blockedReason`). `list --blocked` shows unfinished tasks blocked by a
typed blocker **or** an incomplete dependency; `list --blocked-on human` only the
former. Work waiting on other backlog work still uses `dependsOn`.

### Where the work lives (`workRef`)

`todos update` records `workRef` = `{branch, worktreePath, headCommit, recordedAt}`
automatically when a task moves to `executing` from inside a checkout of the
same repository, and refreshes it on updates to an executing task (or on
completing it) from a linked worktree — unless the task already points at a
different worktree that still exists. Updates from the main checkout that are
not a start leave it alone. `--branch NAME` / `--worktree PATH` set it
explicitly. `todos list --fields key,workRef` shows where unmerged work is.

## Adding work

```bash
waterfree todos add --title "Add rate limiting to /api/auth" --description "Token-bucket rate limiting on the auth endpoint." --priority P1 --owner-type agent --target-file src/api/auth.py --target-line 42
```

`--target-line`: omit for top-of-file, `-1` for end-of-file, else the exact line.
If an off-subject item surfaces mid-task, capture it here for later.

## Deleting and pruning

```bash
waterfree todos delete <id|key>
waterfree todos delete --ids-file stale.txt --dry-run      # one id/key per line, or JSON; '-' = stdin
waterfree todos prune --status complete --older-than 30d --export history/done.json
```

`delete --ids-file` deletes everything in one transaction; if any entry matches
no task, nothing is deleted (exit 3, `notFound` lists them) unless
`--ignore-missing`. It accepts `todos list --fields id` output directly. Remaining
tasks that depended on a deleted task are listed in `orphanedDependents`.

`prune` removes finished work (`--status complete|skipped`, repeatable; default
`complete`). It writes the tasks to `--export FILE` first — merged into an
existing export, never overwriting it — and deletes nothing if that fails.
`--export` is required unless `--dry-run`. `--older-than 30d|12h|2w` measures
from `completedAt`; tasks closed before dates were recorded have none and are
skipped (`skippedUndated`) unless `--include-undated`. Dependencies on pruned
tasks are removed from the remaining tasks (`detachedDependencies`) so they stay
ready. The export file is a valid `todos import --file` input.

## Exit codes

`todos update <id> --quiet` emits a compact JSON receipt containing `id` and
`status` (plus any `warnings`), retaining the complete task and notes in
storage. Use it for updates whose full description and note history would
consume unnecessary context.

`0` ok · `2` usage/validation · `3` task not found · `1` internal error.
