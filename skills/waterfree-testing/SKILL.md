---
name: waterfree-testing
description: Use the `waterfree testing` CLI to run tests, inspect failures, and retrieve logs — without reading raw terminal output.
---

# WaterFree — Test Runner

Provides a clean interface for running tests in any supported framework via the
`waterfree` CLI. Auto-detects the framework from the project
(godot → pytest → jest → vitest → unittest).

Each invocation is a short shell command — run it in whatever shell you have
(Bash or PowerShell). `waterfree` is on PATH, so the command text is identical
in both. All commands emit JSON to stdout (except `logs`, which prints raw test
output).

## When to Use

- Verify that existing tests still pass after a change — `waterfree testing run`
- Run a specific test to confirm a fix — `waterfree testing run-one <substr>`
- See which tests exist before running one — `waterfree testing list`
- Read the full output of the last test run — `waterfree testing logs`
- Triage a wall of failures into root causes — `waterfree testing summarize`

## CLI

All commands accept `--workspace <path>` (defaults to CWD). JSON-producing
commands also accept `--full` for cross-area CLI compatibility.

`run`, `run-one` and `list` additionally accept:

- `--runner {godot,jest,pytest,unittest,vitest}` — skip auto-detection and force
  a framework. Useful in a polyglot repo where detection picks the wrong one.
- `--godot-path <exe>` — the Godot executable to use. See *Godot* below.

### Run all tests
```bash
waterfree testing run --workspace .
```
Output shape:
```json
{
  "passed": 42,
  "failed": 0,
  "total": 42,
  "results": [ { "name": "...", "passed": true, "error": null, "duration_ms": 12.3 } ]
}
```
Exit code is `0` if all tests pass, `1` if any failed.

### Run one or more matching tests
```bash
waterfree testing run-one "test_foo" --workspace .
```
Case-insensitive substring match. Same JSON shape as `run`. Exit `0` only when
at least one test matched and none failed.

### Discover all test names
```bash
waterfree testing list --workspace .
```
Returns a JSON array of test name strings.

### Get full logs from the last run
```bash
waterfree testing logs --workspace .
```
Prints raw stdout+stderr from the most recent `run` or `run-one` (not JSON).
Use this after a failure to see the complete traceback.

### Intelligent failure summary

A red suite usually fails in fewer ways than it has failing tests. Instead of
reading thirty tracebacks, ask for a root-cause grouping:

```bash
waterfree testing run --summary --workspace .      # summarize this run
waterfree testing summarize --workspace .          # summarize the stored last run
```

`--summary` adds a `summary` string to the same JSON; `summarize` returns
`{"summary": "..."}` without re-running the suite. This is advisory: the summary
never changes the exit code, and if the model is unreachable the run still
reports its real result with the reason in `summaryError`.

The model comes from the `testing` stage in `.waterfree/providers.json`. On an
OpenRouter provider it defaults to `auto:free` — the widest-context zero-priced
model available right now — falling through to the cheapest paid model and then
to local Ollama if that is rate limited. Use `auto:floor` for cheapest-paid-first
instead, or pin a concrete model id. See `docs/cli-surface.md`.

Prefer `logs` when you need the literal traceback, and `summarize` when you need
to know *which* problem to fix first.

## Recommended pattern

```bash
waterfree testing run --workspace .                  # Quick pass/fail summary
# if failing:
waterfree testing summarize --workspace .            # Root causes, most impactful first
waterfree testing logs --workspace .                 # Full traceback
# fix code, then:
waterfree testing run-one "test_foo" --workspace .   # Confirm specific test passes
waterfree testing run --workspace .                  # Verify nothing else broke
```

## Supported frameworks

| Framework | Auto-detected by |
|-----------|-----------------|
| Godot     | `project.godot` **and** `addons/gdUnit4/` or `addons/gut/` |
| pytest    | `pytest.ini`, `conftest.py`, `[tool.pytest]` in pyproject.toml |
| Jest      | `jest.config.*`, `"jest"` in package.json |
| Vitest    | `vitest.config.*`, `"vitest"` in package.json |
| unittest  | fallback (default for WaterFree itself) |

Godot is checked first because it needs two signals at once, so it never fires
on a project that merely sits next to a Godot install.

## Godot

Both mainstream Godot test frameworks are supported, picked by which addon the
project installs — **gdUnit4** (`addons/gdUnit4/`) is preferred over **GUT**
(`addons/gut/`) when both are present. Tests are expected in `res://test` or
`res://tests`.

The project does not have to sit at the workspace root: `project.godot` is
looked for at the root and then one level down, which covers the common layout
of keeping the engine build and the game project as siblings.

`list` and a non-matching `run-one` read the `.gd` sources directly and never
boot the engine, so they are fast.

### Finding the engine

Godot ships many differently named builds (`godot.windows.editor.double.x86_64.exe`,
`Godot_v4.3-stable`, …), so WaterFree never guesses by globbing. Resolution
order:

1. `--godot-path <exe>`
2. the `waterfree.godotPath` VS Code setting (mirrored to `.waterfree/config.json`)
3. `$WATERFREE_GODOT`, `$GODOT_BIN`, `$GODOT`
4. `godot`, `godot4`, or `Godot` on PATH

A configured path that does not exist is a hard error — it never quietly falls
through to PATH, because running a different engine build than intended is
worse than failing loudly.

Godot 4 is the target. A Godot 3 binary is detected via `--version` and driven
with `--no-window` instead of `--headless`, but neither modern GUT nor gdUnit4
supports Godot 3, so this is a courtesy rather than a supported path.

Slow suites: raise the 600s default with `WATERFREE_GODOT_TIMEOUT=<seconds>`.

Setup problems (no engine, no `project.godot`, no test addon) exit **4**, so
they stay distinguishable from "your tests are red" (exit 1).

## Workspace

Always pass the absolute path to the project root via `--workspace`, or run the
command from the project root. Test logs are stored at
`{workspace}\.waterfree\testing\last_run.log`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | All tests passed |
| 1    | One or more tests failed |
| 2    | Usage / validation error |
| 4    | Runner setup problem (e.g. Godot engine, project, or test addon not found) |
