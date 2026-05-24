---
name: waterfree-todos
description: Use the `waterfree todos` CLI to inspect the workspace backlog, find ready work, add tasks, and update task status as implementation progresses.
---

# WaterFree — Task / Todo Store

You have access to the workspace task backlog via the `waterfree` CLI. Each
invocation is a short shell command — run it through Bash. Tasks are stored
per-workspace in `.waterfree/tasks.db` (SQLite).

All commands emit JSON to stdout; parse with `json.loads` rather than grepping.

## When to Use

- See what work is planned or in progress — `waterfree todos list`
- Find tasks related to a specific area — `waterfree todos search <query>`
- Record a new piece of work that was identified — `waterfree todos add`
- Mark a task complete after finishing it — `waterfree todos update <id> --patch '{"status":"complete"}'`
- Check what to work on next (highest-priority, no blockers) — `waterfree todos get-next`
- See only unblocked work — `waterfree todos get-ready`
- If an off-subject item needs to be addressed later, push it into todos for future work.

## Task Model

Each task has:

**Identity & classification**
- `id` — UUID (auto-generated)
- `title` — one-line summary
- `description` — full description of what needs to be done
- `rationale` — *why* this task exists (motivation, not steps)
- `taskType` — `impl` | `test` | `spike` | `review` | `refactor` | `protocol` | `bug_fix` | `feature` | `task`
- `phase` — optional milestone/sprint label for grouping

**Priority & status**
- `priority` — `P0` (blocker) | `P1` (critical path) | `P2` (default) | `P3` (backlog) | `spike` (research)
- `status` — `pending` | `executing` | `complete` | `skipped`

**Scheduling & recurrence**
- `timing` — `one_time` (default) | `recurring`
  - Recurring tasks auto-reset to `pending` when marked `complete`.
- `trigger` — free-text description of *what event or condition* should prompt re-evaluation.

**Completion gate**
- `acceptanceCriteria` — free-text definition of done.

**Ownership**
- `owner` — `{ type: "human"|"agent"|"unassigned", name: "..." }`

**Location anchors**
- `targetCoord` — primary file/line the task applies to.
- `contextCoords` — additional file/line anchors for related context.

**Dependencies**
- `dependsOn` — list of `{ taskId, type }` entries
  - `type: "blocks"` — hard dependency.
  - `type: "informs"` — soft: that task's output changes how this task is done.
  - `type: "shares-file"` — warns of conflict risk if worked in parallel.

**Effort tracking**
- `estimatedMinutes` / `actualMinutes` — optional.

**Notes**
- `humanNotes` — notes from a human for the implementor.
- `aiNotes` — notes from an agent (observations, blockers, progress).

## CLI

Every command accepts `--workspace <path>` (defaults to CWD).

### List tasks
```bash
waterfree todos list --workspace /abs/path/to/project
waterfree todos list --workspace . --status pending
waterfree todos list --workspace . --priority P0
waterfree todos list --workspace . --phase v2-launch
waterfree todos list --workspace . --owner agent --ready-only
```

### Search tasks
```bash
waterfree todos search "authentication" --workspace .
waterfree todos search "database migration" --workspace . --limit 10
```
Matches title, description, rationale, file paths, owner, acceptance criteria, and trigger.

### Next task to work on
```bash
waterfree todos get-next --workspace .
waterfree todos get-next --workspace . --owner agent
```
Returns the highest-priority task with no blocking dependencies, or `null`.

### Ready tasks (no blockers)
```bash
waterfree todos get-ready --workspace . --limit 10
```

### Add a task
```bash
waterfree todos add --workspace . \
    --title "Add rate limiting to /api/auth" \
    --description "Implement token-bucket rate limiting on the auth endpoint." \
    --priority P1 \
    --phase security-hardening \
    --owner-type agent \
    --target-file src/api/auth.py \
    --target-line 42
```

`--target-line` behaviour:
- omitted — top of file (no line anchor)
- `-1` — end of file
- positive integer — that exact line number

### Update a task
```bash
waterfree todos update --workspace . <task-id> \
    --patch '{"status":"complete","actualMinutes":45}'
```

Supported patch keys: `title`, `description`, `rationale`, `priority`, `phase`,
`status`, `taskType`, `timing`, `trigger`, `acceptanceCriteria`, `owner`,
`blockedReason`, `humanNotes`, `aiNotes`, `estimatedMinutes`, `actualMinutes`,
`targetCoord`, `dependsOn`, `contextCoords`, `startedAt`, `completedAt`.

### Delete a task
```bash
waterfree todos delete --workspace . <task-id>
```

## Recurring task pattern

```bash
# 1. Create the task
waterfree todos add --workspace . --title "Review test coverage" \
    --description "Check that critical paths still have coverage."

# 2. Patch it to recurring
waterfree todos update --workspace . <task-id> \
    --patch '{"timing":"recurring","trigger":"after each release"}'

# 3. Mark complete when done — auto-resets to pending
waterfree todos update --workspace . <task-id> \
    --patch '{"status":"complete","actualMinutes":15}'
```

## Tips

- Always call `get-next` before starting new work — don't duplicate effort.
- Use `list --status executing` to see what's actively in progress.
- When you finish a task, immediately update its status to `"complete"`.
- Priority order: P0 > P1 > P2 > P3 > spike.
- Use `acceptanceCriteria` when the definition of done is non-obvious.
- Use `aiNotes` to leave breadcrumbs about what you discovered or why you stopped.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 2    | Usage / validation error |
| 3    | Not found (task id) |
| 1    | Internal error |
