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

Output is **compact** by default — null/empty/default fields are omitted (no
`owner` ⇒ unassigned, no `timing` ⇒ one_time, no `taskType` ⇒ impl). Add `--full`
only when you specifically need the raw shape. `list`, `search`, and `get-ready`
return a consistent envelope: `{ "tasks": [...], "total": N, ... }`.

Anywhere a command takes a task id, the task's stable `key` (e.g. `GOV-001`,
case-insensitive) works too: `get`, `update`, `delete`.

## Reading — start here

```bash
waterfree todos get-next                 # the one task to work on now (or null)
waterfree todos get-ready --limit 5      # the next few unblocked tasks, by priority
waterfree todos search "auth rate limit" # find tasks containing all of those words
waterfree todos get GOV-001              # one task, by key or id
waterfree todos validate                 # check backlog consistency
```

`get-next` returns the highest-priority unblocked task. **Call it before starting
work** so you don't duplicate effort. Reach for `get-ready`/`search` when you need
more than one candidate, and `get <id|key>` to resolve a `dependsOn.taskId` or a
key to its task.
Run `validate` when task output looks inconsistent; it reports missing fields,
duplicate keys, unresolved dependencies, cycles, and tasks that still have a
blocked reason despite being ready.

### How `search` matches

- Every word must appear somewhere in the task (any field, **any order**):
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
> tight `--status`/`--priority`/`--limit` filter) when you truly need a full survey.
> `list` always reports the true `total`; when the page is cut it sets
> `"truncated": true` and `nextOffset` (use `--offset N` or a larger `--limit`)
> and prints a note on stderr.

## Updating a task — no JSON needed

Use discrete flags for the common writes — they sidestep shell-quoting problems:

```bash
waterfree todos update <id> --status complete          # close a task
waterfree todos update <id> --status executing         # mark it in progress
waterfree todos update <id> --priority P1 --phase v2
waterfree todos update <id> --append-ai-notes "2026-09-25: blocked on missing migration"
waterfree todos update <id> --owner-type agent --owner-name claude
```

Flags: `--status` `--priority` `--phase` `--owner-type` `--owner-name`
`--append-ai-notes` `--append-human-notes` `--ai-notes` `--human-notes`
`--actual-minutes`. Statuses:
`pending | executing | complete | skipped` (plus `annotating | negotiating` for
the annotation flow). Priorities: `P0 | P1 | P2 | P3 | spike` (P0 highest).

**Notes:** `--append-ai-notes` / `--append-human-notes` add a paragraph and keep
what is already there — use these for dated breadcrumbs. `--ai-notes` /
`--human-notes` **replace** the whole field; the CLI warns on stderr when that
throws away more than half of the earlier text.

For a field without a flag, use `--patch '<json>'` (discrete flags win on conflict).
Recurring tasks (`timing: recurring`) auto-reset to `pending` when set `complete`.

- When you finish work, immediately `update <id> --status complete`.
- Leave breadcrumbs with `--append-ai-notes` when you stop or hit a blocker.

## Adding work

```bash
waterfree todos add --title "Add rate limiting to /api/auth" --description "Token-bucket rate limiting on the auth endpoint." --priority P1 --owner-type agent --target-file src/api/auth.py --target-line 42
```

`--target-line`: omit for top-of-file, `-1` for end-of-file, else the exact line.
If an off-subject item surfaces mid-task, capture it here for later.

## Deleting

```bash
waterfree todos delete <id|key>
```

## Exit codes

`0` ok · `2` usage/validation · `3` task not found · `1` internal error.
