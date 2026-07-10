# WaterFree CLI Surface

Replaces the MCP stdio servers with a single compiled executable that exposes
each former MCP tool as a `waterfree <area> <action>` subcommand. The goal:
agents call it via the shell, no MCP transport involved, and the executable
ships with every dependency frozen in via PyInstaller — no Python required on
the host.

## Invocation shape

```
waterfree <area> <action> [--workspace <path>] [flags] [positional]
```

- **area** — one of: `todos`, `knowledge`, `index`, `testing`, `qa-summary`
- **action** — area-specific verb (e.g. `list`, `add`, `search`, `delete`)
- **--workspace** — path to the project root. Defaults to CWD. Required for
  every `todos`, `index`, and `testing` action (knowledge is global).
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
| `list`       | `--status`, `--priority`, `--phase`, `--owner`, `--ready-only`, `--limit N`, `--full` | `list_tasks` |
| `search`     | `<query>` (positional), `--limit N`, `--full`                                      | `search_tasks` |
| `get-next`   | `--owner NAME`, `--full`                                                           | `get_next_task` |
| `get-ready`  | `--limit N`, `--full`                                                              | `get_ready_tasks` |
| `add`        | `--title T`, `--description D`, `--key`, `--priority`, `--phase`, `--owner-type`, `--target-file`, `--target-line`, `--full` | `add_task` |
| `update`     | `<task-id>`, `--status`, `--priority`, `--phase`, `--owner-type`, `--owner-name`, `--ai-notes`, `--human-notes`, `--actual-minutes`, `--patch '<json>'`, `--full` | `update_task` |
| `delete`     | `<task-id>`                                                                        | `delete_task` |
| `import`     | `--file <path\|->`, `--upsert`, `--dry-run`, `--full`                             | — (bulk `add_task`/`update_task`) |

All actions accept `--workspace` (default: CWD). Read/write actions emit
**compact** JSON (null/empty/default fields omitted) unless `--full` is passed.
On `update`, discrete flags cover the common edits without JSON; `--patch` is for
fields without a flag and discrete flags win on conflict.

Tasks have an optional stable `key` (e.g. `GOV-001`), settable via `add --key`
or `update --patch '{"key": "..."}'`. It must be unique across the workspace
(`add`/`update` reject a collision with exit code 2). Entries in `dependsOn`
accept `{"key": "GOV-001", "type": "blocks"}` as an alternative to
`{"taskId": "<uuid>", "type": "blocks"}` — resolved to the real id at write
time, so tasks can reference each other by a name you chose instead of a
generated UUID. `search`/`list`/`get-*` include `key` in their output when set.

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
`~/.waterfree/global/knowledge.db` — no `--workspace`.

| Action          | Flags / args |
|-----------------|--------------|
| `search`        | `<query>`, `--limit N` |
| `browse`        | `--path P`, `--depth N`, `--include-entries`, `--entry-limit N` |
| `add`           | `--title`, `--description`, `--code-file PATH` (or `--code -` for stdin), `--snippet-type`, `--source-repo`, `--source-file`, `--tag T` (repeatable), `--context`, `--source-repo-url`, `--hierarchy-path` |
| `delete`        | `<entry-id>` |
| `list-sources`  | — |
| `stats`         | — |

Note: `--code-file` is the preferred way to pass a snippet body; shell-escaping
multi-line code through argv is painful. `--code -` reads from stdin.

## Area: index

Mirrors `backend/mcp_index.py`. Backed by `GraphClient` against the workspace
graph DB.

| Action               | Flags / args |
|----------------------|--------------|
| `build`              | (alias: `index`) — full index |
| `status`             | — |
| `search-code`        | `<query>`, `--max N` |
| `search-graph`       | `<query>`, `--node-type T`, `--limit N` |
| `get-snippet`        | `<qualified-name>`, `--scope procedure|neighbors|class` |
| `trace`              | `<function>`, `--direction callers|callees|both`, `--depth N` |
| `detect-changes`     | `--scope all|<files>`, `--depth N` |
| `architecture`       | — |
| `list-projects`      | — (no `--workspace`) |

All actions except `list-projects` accept `--workspace`.

## Area: testing

Mirrors `backend/mcp_testing.py`. Auto-detects unittest/pytest/jest/vitest.

| Action       | Flags / args |
|--------------|--------------|
| `run`        | — (runs all) |
| `run-one`    | `<name-substring>` |
| `list`       | — |
| `logs`       | — |

## Area: qa-summary

Mirrors `backend/mcp_qa_summary.py`. Requires local Ollama.

| Action       | Flags / args |
|--------------|--------------|
| `ask`        | `<file-or-url>`, `--question Q` (or `-q`) |

Exit code `4` if Ollama isn't running or the configured model is missing.

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
