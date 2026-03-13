---
name: waterfree-todos
description: Use the WaterFree task MCP tools to inspect the workspace backlog, find ready work, add tasks, and update task status as implementation progresses.
---

# WaterFree — Task / Todo Store Tools

You have access to the workspace task backlog via the `waterfree-todos` MCP server.
Tasks are stored per-workspace in `.waterfree/tasks.db` (SQLite). 

## When to Use

Use these tools when you need to:
- See what work is planned or in progress — use `list_tasks` or `get_next_task`
- Find tasks related to a specific area — use `search_tasks`
- Record a new piece of work that was identified — use `add_task`
- Mark a task complete after finishing it — use `update_task` with `"status": "complete"`
- Check what to work on next (highest-priority, no blockers) — use `get_next_task`
- See only unblocked work — use `get_ready_tasks`
- Create a recurring health check or review — use `add_task` then patch `timing` to `"recurring"`
- If an off subject item needs to be addressed later, push it into todos for future work
- Future work or Suggestions for improvements

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
- `priority` — `P0` (blocker) | `P1` (critical path) | `P2` (default, this session) | `P3` (backlog) | `spike` (research, no code)
- `status` — `pending` | `executing` | `complete` | `skipped`

**Scheduling & recurrence**
- `timing` — `one_time` (default) | `recurring`
  - **Recurring tasks auto-reset to `pending` when marked `complete`.** Use this for periodic checks, weekly reviews, or ongoing health monitors that should recur indefinitely.
- `trigger` — free-text description of *what event or condition* should prompt re-evaluation (e.g. `"after each release"`, `"when coverage drops below 80%"`). Most useful with `recurring`.

**Completion gate**
- `acceptanceCriteria` — free-text definition of done. Describe what must be true for the task to be considered finished. Used in search and displayed to implementors.

**Ownership**
- `owner` — `{ type: "human"|"agent"|"unassigned", name: "..." }`

**Location anchors**
- `targetCoord` — primary file/line the task applies to: `{ file, line, class, method, anchorType }`
- `contextCoords` — additional file/line anchors for related context (array of same shape)

**Dependencies**
- `dependsOn` — list of `{ taskId, type }` entries
  - `type: "blocks"` — hard dependency: cannot start until that task completes
  - `type: "informs"` — soft: that task's output changes how this task is done
  - `type: "shares-file"` — warns of conflict risk if worked in parallel

**Effort tracking**
- `estimatedMinutes` / `actualMinutes` — optional time tracking

**Notes**
- `humanNotes` — notes written by a human for the implementor
- `aiNotes` — notes written by an agent (observations, blockers, progress)

## Tools

### List tasks
```
list_tasks(workspace_path="/absolute/path/to/project")
list_tasks(workspace_path="...", status="pending")
list_tasks(workspace_path="...", priority="P0")
list_tasks(workspace_path="...", phase="v2-launch")
list_tasks(workspace_path="...", owner="agent", ready_only=true)
```

### Search tasks
```
search_tasks(workspace_path="/absolute/path/to/project", query="authentication")
search_tasks(workspace_path="...", query="database migration")
```
Searches title, description, rationale, file paths, owner, `acceptanceCriteria`, and `trigger`.

### Get the next task to work on
```
get_next_task(workspace_path="/absolute/path/to/project")
get_next_task(workspace_path="...", owner="agent")
```
Returns the highest-priority task with no blocking dependencies.

### List only ready tasks (no blockers)
```
get_ready_tasks(workspace_path="/absolute/path/to/project", limit=10)
```

### Add a task
```
add_task(
    workspace_path="/absolute/path/to/project",
    title="Add rate limiting to /api/auth",
    description="Implement token-bucket rate limiting on the auth endpoint to prevent brute force.",
    priority="P1",
    phase="security-hardening",
    owner_type="agent",
    target_file="src/api/auth.py"
)
# Pin to a specific line:
add_task(..., target_file="src/api/auth.py", target_line=42)
# Pin to end of file:
add_task(..., target_file="src/api/auth.py", target_line=-1)
```

`target_line` behaviour:
- Omitted / `null` — top of file (no line anchor)
- `-1` — end of file
- positive integer — that exact line number

After creating a task, use `update_task` to set `taskType`, `timing`, `trigger`,
`acceptanceCriteria`, or `dependsOn` if needed.

### Update a task
```
update_task(
    workspace_path="/absolute/path/to/project",
    task_id="<uuid>",
    patch='{"status": "complete", "actualMinutes": 45}'
)
```

**All supported patch keys:**

| Key | Type | Notes |
|-----|------|-------|
| `title` | string | |
| `description` | string | |
| `rationale` | string | |
| `priority` | string | `P0`\|`P1`\|`P2`\|`P3`\|`spike` |
| `phase` | string | |
| `status` | string | `pending`\|`executing`\|`complete`\|`skipped` |
| `taskType` | string | `impl`\|`test`\|`spike`\|`review`\|`refactor`\|`protocol`\|`bug_fix`\|`feature`\|`task` |
| `timing` | string | `one_time`\|`recurring` |
| `trigger` | string | When/why the task recurs |
| `acceptanceCriteria` | string | Definition of done |
| `owner` | object | `{ "type": "agent", "name": "..." }` |
| `blockedReason` | string | Why this task can't proceed |
| `humanNotes` | string | Notes from a human |
| `aiNotes` | string | Notes from the agent |
| `estimatedMinutes` | int | |
| `actualMinutes` | int | |
| `targetCoord` | object | `{ "file": "...", "line": 42, "anchorType": "modify" }` |
| `dependsOn` | array | `[{ "taskId": "...", "type": "blocks" }]` |
| `contextCoords` | array | Additional file/line anchors |
| `startedAt` | string | ISO timestamp |
| `completedAt` | string | ISO timestamp |

### Delete a task
```
delete_task(workspace_path="/absolute/path/to/project", task_id="<uuid>")
```

## Recurring task pattern

To create a recurring check (e.g. review test coverage after every sprint):
```
# 1. Create the task
add_task(workspace_path="...", title="Review test coverage", description="...")

# 2. Make it recurring with a trigger
update_task(workspace_path="...", task_id="<uuid>",
    patch='{"timing": "recurring", "trigger": "after each release"}')

# 3. When done for this cycle, mark complete — it auto-resets to pending
update_task(workspace_path="...", task_id="<uuid>",
    patch='{"status": "complete", "actualMinutes": 15}')
# → status is immediately reset to "pending" by the server
```

## Tips

- Always call `get_next_task` before starting new work — don't duplicate effort.
- Use `list_tasks(status="executing")` to see what's actively in progress.
- When you finish a task, immediately update its status to `"complete"`.
- Priority order: P0 > P1 > P2 > P3 > spike.
- Use `acceptanceCriteria` when the definition of done is non-obvious — it surfaces in search.
- Use `aiNotes` to leave breadcrumbs about what you discovered or why you stopped.
- `dependsOn` with `type: "informs"` (soft) won't block scheduling but signals to read that task's output first.
