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

## Task Model

Each task has:
- `id` — UUID
- `title` — one-line summary
- `description` — full description
- `priority` — `P0` (critical) | `P1` | `P2` (default) | `P3` | `spike`
- `status` — `pending` | `ready` | `executing` | `complete` | `skipped`
- `owner` — `{ type: "human"|"agent"|"unassigned", name: "..." }`
- `phase` — optional milestone label
- `targetCoord` — optional file/line the task applies to
- `dependsOn` — list of blocking task IDs

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

### Update a task
```
update_task(
    workspace_path="/absolute/path/to/project",
    task_id="<uuid>",
    patch='{"status": "complete", "actualMinutes": 45}'
)
```
Supported patch keys: `title`, `description`, `rationale`, `priority`, `phase`, `status`,
`owner` (object), `blockedReason`, `humanNotes`, `aiNotes`, `estimatedMinutes`, `actualMinutes`.

Valid status values: `pending`, `ready`, `executing`, `complete`, `skipped`

### Delete a task
```
delete_task(workspace_path="/absolute/path/to/project", task_id="<uuid>")
```

## Tips

- Always call `get_next_task` before starting new work — don't duplicate effort.
- Use `list_tasks(status="executing")` to see what's actively in progress.
- When you finish a task, immediately update its status to `"complete"`.
- Priority order: P0 > P1 > P2 > P3 > spike.
