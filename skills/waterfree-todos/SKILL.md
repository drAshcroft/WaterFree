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
return a consistent envelope: `{ "tasks": [...], "total": N }`.

## Reading — start here

```bash
waterfree todos get-next                 # the one task to work on now (or null)
waterfree todos get-ready --limit 5      # the next few unblocked tasks, by priority
waterfree todos search "auth rate limit" # find a specific task by text
waterfree todos validate                 # check backlog consistency
```

`get-next` returns the highest-priority unblocked task. **Call it before starting
work** so you don't duplicate effort. Reach for `get-ready`/`search` when you need
more than one candidate.
Run `validate` when task output looks inconsistent; it reports missing fields,
duplicate keys, unresolved dependencies, cycles, and tasks that still have a
blocked reason despite being ready.

> **Avoid `waterfree todos list`.** It dumps up to 50 tasks and burns tokens fast.
> Use `get-next` / `get-ready` / `search` instead. Only fall back to `list` (with a
> tight `--status`/`--priority`/`--limit` filter) when you truly need a full survey.

## Updating a task — no JSON needed

Use discrete flags for the common writes — they sidestep shell-quoting problems:

```bash
waterfree todos update <id> --status complete          # close a task
waterfree todos update <id> --status executing         # mark it in progress
waterfree todos update <id> --priority P1 --phase v2
waterfree todos update <id> --ai-notes "blocked on missing migration"
waterfree todos update <id> --owner-type agent --owner-name claude
```

Flags: `--status` `--priority` `--phase` `--owner-type` `--owner-name`
`--ai-notes` `--human-notes` `--actual-minutes`. Statuses:
`pending | executing | complete | skipped` (plus `annotating | negotiating` for
the annotation flow). Priorities: `P0 | P1 | P2 | P3 | spike` (P0 highest).

For a field without a flag, use `--patch '<json>'` (discrete flags win on conflict).
Recurring tasks (`timing: recurring`) auto-reset to `pending` when set `complete`.

- When you finish work, immediately `update <id> --status complete`.
- Leave breadcrumbs with `--ai-notes` when you stop or hit a blocker.

## Adding work

```bash
waterfree todos add --title "Add rate limiting to /api/auth" --description "Token-bucket rate limiting on the auth endpoint." --priority P1 --owner-type agent --target-file src/api/auth.py --target-line 42
```

`--target-line`: omit for top-of-file, `-1` for end-of-file, else the exact line.
If an off-subject item surfaces mid-task, capture it here for later.

## Deleting

```bash
waterfree todos delete <id>
```

## Exit codes

`0` ok · `2` usage/validation · `3` task not found · `1` internal error.
