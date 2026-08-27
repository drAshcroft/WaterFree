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

- **area** — one of: `todos`, `knowledge`, `index`, `testing`, `qa-summary`,
  `vision`, `imagegen`
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
| `list`       | `--status`, `--priority`, `--phase`, `--owner`, `--ready-only`, `--limit N`, `--full` | `list_tasks` |
| `search`     | `<query>` (positional), `--limit N`, `--full`                                      | `search_tasks` |
| `get-next`   | `--owner NAME`, `--full`                                                           | `get_next_task` |
| `get-ready`  | `--limit N`, `--full`                                                              | `get_ready_tasks` |
| `schema`     | `--workspace`                                                                      | - (task schema) |
| `task-types` | `--workspace`                                                                      | - (enum discovery) |
| `validate`   | `--workspace`                                                                      | - (backlog validation) |
| `add`        | `--title T`, `--description D`, `--key`, `--priority`, `--phase`, `--owner-type`, `--target-file`, `--target-line`, `--json-file <path\|->`, `--full` | `add_task` |
| `update`     | `<task-id>`, `--status`, `--priority`, `--phase`, `--owner-type`, `--owner-name`, `--ai-notes`, `--human-notes`, `--actual-minutes`, `--patch '<json>'`, `--patch-file <path\|->`, `--full` | `update_task` |
| `delete`     | `<task-id>`                                                                        | `delete_task` |
| `import`     | `--file <path\|->`, `--upsert`, `--dry-run`, `--full`                             | — (bulk `add_task`/`update_task`) |

All actions accept `--workspace` (default: CWD). Read/write actions emit
**compact** JSON (null/empty/default fields omitted) unless `--full` is passed.
`list`, `search`, and `get-ready` all return `{ "tasks": [...], "total": N }`
envelopes; `list` also includes `phases`. On `update`, discrete flags cover the
common edits without JSON; `--patch` is for fields without a flag and discrete
flags win on conflict. `add --json-file` reads one complete task object, while
`update --patch-file` reads one JSON patch object; both accept `-` for stdin.
All JSON file and stdin inputs use UTF-8 (with an optional UTF-8 BOM); malformed
UTF-8 and invalid Unicode return a usage error before any task is persisted.
When combined, file data is applied first, then inline JSON and discrete flags
override it. File paths are resolved relative to CWD.
`schema` prints the complete task JSON schema, including valid enum values.
`task-types` prints the accepted `taskType` values. Invalid enum values reported
by task writes include the accepted values inline.
`validate` checks the persisted backlog for missing required fields, duplicate
keys, unresolved dependencies, dependency cycles, title-convention warnings, and
tasks that carry a `blockedReason` but would otherwise appear ready. It returns
`{ "ok": bool, "issueCount": N, "errorCount": N, "warningCount": N, "issues": [...] }`
and exits `2` when errors are present.

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
`~/.waterfree/global/knowledge.db`; `--workspace` is accepted for agent
muscle-memory compatibility but does not change the global store location.

| Action          | Flags / args |
|-----------------|--------------|
| `search`        | `<query>`, `--limit N`, `--workspace`, `--full` |
| `browse`        | `--path P`, `--depth N`, `--include-entries`, `--entry-limit N`, `--workspace`, `--full` |
| `add`           | `--title`, `--description`, `--code-file PATH` (or `--code -` for stdin), `--snippet-type`, `--source-repo`, `--source-file`, `--tag T` (repeatable), `--context`, `--source-repo-url`, `--hierarchy-path`, `--workspace` |
| `delete`        | `<entry-id>`, `--workspace` |
| `list-sources`  | `--workspace`, `--full` |
| `stats`         | `--workspace`, `--full` |

Note: `--code-file` is the preferred way to pass a snippet body; shell-escaping
multi-line code through argv is painful. `--code -` reads from stdin.
`knowledge search` returns `{ "entries": [...], "total": N }`.

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
| `run`        | `--workspace`, `--full`, `--summary` |
| `run-one`    | `<name-substring>`, `--workspace`, `--full`, `--summary` |
| `list`       | `--workspace`, `--full` |
| `logs`       | — |
| `summarize`  | `--workspace` |

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

## Reader-stage model selection

`qa_summary`, `tutorial`, and `testing` are the *reader* stages. A provider must
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
the defaults for the three reader stages on OpenRouter — set a concrete model id
to opt out. See `backend/llm/openrouter_catalog.py`.

Exit code `4` if the resolved provider is unavailable — Ollama not running, the
model not installed, or no API key for a remote provider.

## Area: vision

Implemented in `backend/vision/`. Runs a local vision model through the Ollama
daemon; no image leaves the machine. Takes image **files** — it does not capture
screenshots itself.

| Action      | Flags / args |
|-------------|--------------|
| `look`      | `<image>...` (up to 4), `--purpose`, `-q/--question`, `--model`, `--tier`, `--workspace` |
| `models`    | `--workspace` |
| `purposes`  | `--workspace` |
| `pull`      | `--tier {small,large}` or `--model <id>`, `--workspace` |

Purposes select both the model tier and the framing of the question:
`describe` (small), and `triage` / `ui` / `function` / `text` / `compare` (large).

Two tiers: `small` = moondream (~1.7 GB), `large` = qwen2.5vl:7b (~6 GB).
Override per-invocation with `--model`, or globally with
`$WATERFREE_VISION_MODEL_SMALL` / `$WATERFREE_VISION_MODEL_LARGE`.

**Nothing is downloaded implicitly.** A missing model exits `4` with the exact
`waterfree vision pull` command — these are gigabytes, and a describe should
never silently cost 6 GB of disk.

Images are downscaled to 1280px on the long edge (`$WATERFREE_VISION_MAX_EDGE`)
and re-encoded as PNG before being sent.

## Area: imagegen

Implemented in `backend/imagegen/`. Generates images on the local GPU via
`diffusers`. Requires `diffusers`, `accelerate`, `sentencepiece`, `protobuf`
and a CUDA-capable PyTorch; CPU generation is deliberately unsupported.

| Action    | Flags / args |
|-----------|--------------|
| `make`    | `<prompt>`, `-n/--count`, `--preset`, `--steps`, `--guidance`, `--width`, `--height`, `--negative-prompt`, `--seed`, `--offload`, `--output-dir`, `--hf-token`, `--workspace` |
| `models`  | `--workspace` |
| `status`  | `--workspace` |
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
