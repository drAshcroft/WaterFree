---
name: waterfree-testing
description: Use the `waterfree testing` CLI to run tests, inspect failures, and retrieve logs — without reading raw terminal output.
---

# WaterFree — Test Runner

Provides a clean interface for running tests in any supported framework via the
`waterfree` CLI. Auto-detects the framework from the project
(godot → pytest → jest → vitest → npm scripts → dotnet → unittest).

**Zero discovered tests is never a pass.** If no framework can be detected, or a
runner runs and finds nothing, the command exits **4** with an explanation
instead of reporting `0 passed, 0 failed` and exit 0. Treat exit 4 as "these
tests did not run", never as evidence that anything is green.

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

- `--runner {dotnet,godot,jest,npm-scripts,playwright,pytest,unittest,vitest}` —
  skip auto-detection and force a framework. Useful in a polyglot repo where
  detection picks the wrong one, and the only way to select `playwright`.
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

**Cold start:** local Ollama (the default when no provider claims the `testing`
stage, and the last-resort fallback otherwise) unloads its model from memory
when idle and has to reload it from disk on the next call — potentially several
minutes for a 14B model. That reload eats into the request's own 180s timeout
with no retry, so `--summary`/`summarize` can fail with `summaryError` on a cold
daemon even though the test run itself is unaffected (summarization never
changes the exit code). Retrying right after usually succeeds since the model
is warm by then. To avoid the wait, warm the model up first with
`ollama run freehuntx/qwen3-coder:14b ""` (blocks until loaded, then returns).

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
| npm scripts | `test:*` (or a bare `test`) script in package.json, with **no** jest/vitest/mocha/playwright dependency |
| .NET      | a `*.sln`, or a `*.csproj` referencing Microsoft.NET.Test.Sdk / xunit / NUnit / MSTest |
| Playwright | **never auto-detected** — select it with `--runner playwright` |
| unittest  | `tests/` or `backend/tests/` containing `test_*.py` (default for WaterFree itself) |

Godot is checked first because it needs two signals at once, so it never fires
on a project that merely sits next to a Godot install.

If nothing matches, the command exits 4 with the full list of what it looked
for. It does **not** fall back to a runner that is guaranteed to find nothing.

### npm scripts

For JS projects with a hand-rolled harness rather than a framework — Paradoxia,
for example, has 48 `test:*` scripts each running one `tsx` suite. Each script
is one unit and they run **in parallel** (the `&&` chain in the aggregate `test`
script would stop at the first failure and hide the rest).

Per-test detail is recovered when the harness prints recognisable `✓` / `✗`
lines; otherwise you get one result per script. Either way the script's exit
code is authoritative — a suite that exits non-zero is reported failed even if
every line parsed looked like a pass.

Tune with `WATERFREE_NPM_WORKERS` (default 6) and `WATERFREE_NPM_TIMEOUT`
(default 300s, per script).

### .NET

Runs `dotnet test` against the solution when there is one, otherwise against
each test project, and parses the TRX log rather than console output — the TRX
schema is stable across SDK and framework versions, console formatting is not.

`list` reads the C# sources for `[Fact]` / `[Theory]` / `[Test]` / `[TestMethod]`
attributes instead of building, so it is instant.

Slow suites: `WATERFREE_DOTNET_TIMEOUT=<seconds>` (default 900).

### Playwright

Opt-in only, via `--runner playwright`. Projects that have Playwright almost
always have a unit suite too (goblinchess has both Vitest and Playwright), and
the unit suite is the faster gate that needs no dev server. Results come from
the JSON reporter written to a file, so app stdout cannot corrupt the report.

**You must start the dev server yourself** unless the project's
`playwright.config` has a `webServer` block.

Slow suites: `WATERFREE_PLAYWRIGHT_TIMEOUT=<seconds>` (default 900).

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
2. `.waterfree/config.json`, checked at the workspace root **and** in the Godot
   project directory. All three spellings are accepted: `"godotPath"`,
   `"waterfree.godotPath"`, and `{"waterfree": {"godotPath": ...}}`
3. `$WATERFREE_GODOT`, `$GODOT_BIN`, `$GODOT`
4. `godot`, `godot4`, or `Godot` on PATH

A configured path that does not exist is a hard error — it never quietly falls
through to PATH, because running a different engine build than intended is
worse than failing loudly.

Godot 4 is the target. A Godot 3 binary is detected via `--version` and driven
with `--no-window` instead of `--headless`, but neither modern GUT nor gdUnit4
supports Godot 3, so this is a courtesy rather than a supported path.

Slow suites: raise the 1800s default with `WATERFREE_GODOT_TIMEOUT=<seconds>`.
A real gdUnit4 suite can run for many minutes; engine boot alone is ~40s.

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
| 4    | Runner setup problem — **the tests did not run**. Missing engine or toolchain (Godot, node, npx, dotnet), no detectable framework, or zero tests discovered. Never read this as a pass. |
