---
name: waterfree-testing
description: Use the `waterfree testing` CLI to run tests, inspect failures, and retrieve logs — without reading raw terminal output.
---

# WaterFree — Test Runner

Provides a clean interface for running tests in any supported framework via the
`waterfree` CLI. Auto-detects the framework from the project
(godot → pytest → jest → vitest → unittest → npm scripts → dotnet; `playwright`
and `godot-script` only when asked for).

**Exit 4 means "no verdict", never a pass and never a red test.** It covers: no
detectable framework, a runner that discovers nothing, a `run-one` whose name
matched nothing, and a run stopped by `--budget` / `--stall`. Exit **1** is
reserved for "a test ran and failed". So a typo'd test name is exit 4 with a
JSON error, not a phantom failure to go and debug.

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
- Bound a run that might hang — `--budget 10m --stall 2m`
- Run a Godot SceneTree harness (`tests/run_all.gd` with lanes) — `--runner godot-script`
- Wait for a long run you started yourself — `waterfree testing await` (never `sleep`)

## CLI

All commands accept `--workspace <path>` (defaults to CWD). JSON-producing
commands also accept `--full` for cross-area CLI compatibility.

`run`, `run-one` and `list` additionally accept:

- `--runner {dotnet,godot,godot-script,jest,npm-scripts,playwright,pytest,unittest,vitest}` —
  skip auto-detection and force a framework. Useful in a polyglot repo where
  detection picks the wrong one, and the only way to select `playwright` or
  `godot-script`.
- `--godot-path <exe>` — the Godot executable to use. See *Godot* below.
- `--budget <duration>` — wall-time limit for the whole run (`90s`, `10m`,
  `1h30m`). Replaces the runner's default timeout, so it can also be *longer*.
- `--stall <duration>` — kill the run after this long with no output at all.
  See *Bounded runs* below.

### Run all tests

Unittest suites use a 900-second subprocess deadline. Set
`WATERFREE_UNITTEST_TIMEOUT` to a positive number of seconds above all adapter
startup/build/test/cleanup deadlines for slower suites. Timeouts return a failed
result and retain partial stdout/stderr in the testing log.
```bash
waterfree testing run --workspace .
```
Output shape:
```json
{
  "runner": "pytest",
  "passed": 42,
  "failed": 0,
  "skipped": 3,
  "total": 45,
  "results": [ { "name": "...", "passed": true, "skipped": false, "error": null, "duration_ms": 12.3 } ],
  "elapsedSeconds": 8.4
}
```
Exit code is `0` if all tests pass, `1` if any failed. Skipped, pending, todo
and disabled tests (Jest/Vitest included) count as `skipped`, never as failures.

### Run one or more matching tests
```bash
waterfree testing run-one "test_foo" --workspace .
```
Substring match (case-insensitive where the framework allows; Jest/Vitest use
their own `-t`, .NET uses `FullyQualifiedName~`). Same JSON shape as `run`.
Exit `0` when at least one test ran and none failed, `1` when one failed.

**Nothing matched → exit 4, not exit 1.** If the filter selects nothing that
actually executes (no such name, or everything it selected was skipped), stdout
is a JSON error instead of a result:

```json
{
  "error": "unittest executed no tests matching 'ParryWindow' (212 tests known to unittest). ...",
  "code": "no_tests_matched",
  "runner": "unittest",
  "filter": "ParryWindow",
  "available": 212,
  "skipped": 0,
  "autoDetected": true,
  "otherRunners": [
    { "runner": "dotnet", "evidence": "combat.sln", "matches": 1,
      "retry": "waterfree testing run-one ParryWindow --runner dotnet" }
  ],
  "hint": "unittest was auto-detected, but 1 dotnet test(s) match this name (combat.sln). Retry with --runner dotnet."
}
```

`otherRunners` appears only when the runner was auto-detected: it names every
other framework this repo has evidence for (a C# test name in a repo that also
has Python tests). `matches` is filled in for runners that can list their tests
without booting anything (dotnet, godot, npm-scripts). Do the `retry` it gives
you rather than debugging a test that never ran.

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

### Bounded runs: `--budget` and `--stall`

A headless engine with a GDScript parse error does not exit — it sits there
silently until *your* tool call times out. Bound any run you are not sure of:

```bash
waterfree testing run --budget 10m --stall 2m --workspace .
```

- `--budget` kills the whole process tree once the run has taken that long
  (shared across every process the runner starts).
- `--stall` kills it after that long with no stdout/stderr output.

A killed run exits **4** and is reported as incomplete — never as passed:

```json
{
  "runner": "godot-script", "passed": 0, "failed": 0,
  "incomplete": true, "reason": "stall",
  "elapsedSeconds": 121.3, "limitSeconds": 120,
  "firstError": "SCRIPT ERROR: Parse Error: Expected end of statement after expression.",
  "firstErrorLocation": "res://src/house/plan.gd:41",
  "error": "Run stopped after 2m1s: the test process printed nothing for 2m (--stall) ..."
}
```

`firstError` / `firstErrorLocation` are the first Godot `SCRIPT ERROR` /
`Parse Error` line and its `file:line` — usually the whole diagnosis. Partial
output is still in `waterfree testing logs`. Runners that finished part of the
work (npm scripts) include those partial results. Pick a `--stall` longer than
the slowest *silent* stretch of a healthy run (engine import, a long suite that
prints only at the end), or leave it off for those.

Limitation: the compiled `waterfree` executable runs plain unittest suites
in-process (no subprocess), so `--budget`/`--stall` cannot interrupt that one
path. Every other runner, and unittest when run from source, is bounded.

### Waiting for a long run: `await`

When you start a long run yourself (in the background, writing to a log), do
not poll it with `sleep` loops or read the whole log. Block once:

```bash
waterfree testing await --log build/godot.log --until "ALL PASS|SUITE\(S\) FAILED" --timeout 9m --pid 12345
```

- Only lines written **after** the call starts count, so a stale log from an
  earlier run cannot match. A log that is truncated or recreated is read from
  its new top. Add `--fresh` when an old process may still be appending to the
  same path (only a newly created/rewritten file then counts).
- Returns when a new line matches `--until`, when `--pid` exits, or at
  `--timeout` (default 30m). **Set `--timeout` below your own tool-call
  timeout** so you get the JSON back instead of being cut off.
- Prints only summary lines (PASS/FAIL/SUMMARY/Parse Error/SCRIPT ERROR/
  "N passed" by default; change with `--summary-pattern`), capped at
  `--max-lines` (60, keeping the first and the last), never the whole log.

```json
{
  "matched": true, "reason": "matched", "elapsedSeconds": 312.4,
  "matchLine": "ALL PASS -- 12 suites, 340 checks, 0 failures, 2 warnings",
  "summaryLines": ["  libraryquick       PASS  (12 checked, 0 failures, 0 warnings)", "..."],
  "summaryLineCount": 13, "bytesRead": 48211, "logExists": true
}
```

Exit `0` when matched; `4` for `pid_exited` or `timeout` (no verdict yet).

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
| Godot SceneTree script | **never auto-detected** — `--runner godot-script --script res://...` (see below) |
| unittest  | `tests/` or `backend/tests/` containing `test_*.py` (default for WaterFree itself) |

Godot is checked first because it needs two signals at once, so it never fires
on a project that merely sits next to a Godot install.

If nothing matches, the command exits 4 with the full list of what it looked
for, plus any opt-in runner it did find evidence for (a Playwright config, a
`tests/*.gd` that `extends SceneTree`) and the flags that select it. It does
**not** fall back to a runner that is guaranteed to find nothing.

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

### Godot SceneTree scripts (`--runner godot-script`)

For harnesses that are a plain `extends SceneTree` script rather than GUT or
gdUnit4 — BrickWild's `tests/run_all.gd` with named lanes, smoke scripts. Do
not call Godot by hand and write your own receipt; run:

```bash
waterfree testing run --script res://tests/run_all.gd --lane lane:geom --stall 3m --workspace .
waterfree testing run-one massing --script res://tests/run_all.gd --workspace .
waterfree testing list --script res://tests/run_all.gd --workspace .
```

- Runs `godot --headless --path <project> --script <script> -- <lanes> <args>`.
  `--script` implies `--runner godot-script`; it can also come from
  `"godotTestScript"` in `.waterfree/config.json`. The engine is found exactly
  as for GUT/gdUnit4 (`--godot-path`, config, env vars, PATH).
- `--lane NAME` (repeatable) and `--arg=VALUE` (repeatable; use the `=` form for
  values starting with `-`) are passed after `--`. `run-one X` passes `X` as one
  more lane/suite argument.
- Lane summary lines `<name>  PASS|FAIL  (n checked, m failures[, k warnings])`
  become one result each, with the `FAIL` lines from that lane's `--- name ---`
  block as the error. A JUnit report written to `$WATERFREE_JUNIT_XML` (always
  set) or `--junit-xml PATH` is preferred when present.
- **No lane summary = incomplete (exit 4, `reason: "no_summary"`), never a
  pass.** The receipt's `outputTail` shows the last lines (e.g. "unknown suite").
  A non-zero native exit with no failing lane is still reported as a failure.
- `receipt` in the JSON: `script`, `command`, native `exitCode`, `elapsedSeconds`,
  `fingerprint` (`head` = git HEAD, `dirty` = tracked files modified), parsed
  `lanes`, the `verdict` line, `junit`, and `receiptFile` — full stdout/stderr
  live there and in `waterfree testing logs`, not in the JSON.
- `list` runs the script with `-- --list-lanes`. Convention: print one
  `LANE <name>` line per lane and exit 0. Scripts that instead answer an unknown
  argument with `known lanes: a, b; known suites: c` (BrickWild does) also work.
- The engine's own timeout (`WATERFREE_GODOT_TIMEOUT`, default 1800s) also ends
  as incomplete. Use `--budget` for longer lanes and `--stall` to fail fast on a
  hang.

## Workspace

Always pass the absolute path to the project root via `--workspace`, or run the
command from the project root. Test logs are stored at
`{workspace}\.waterfree\testing\last_run.log`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | All tests passed (`await`: the pattern matched) |
| 1    | One or more tests ran and failed |
| 2    | Usage / validation error |
| 4    | **No verdict — the tests did not (all) run.** Missing engine or toolchain (Godot, node, npx, dotnet), no detectable framework, zero tests discovered, `run-one` matched nothing (`code: no_tests_matched`), or the run was incomplete (`--budget`/`--stall` kill, godot-script with no lane summary; `incomplete: true`). `await`: timeout or pid exited without a match. Never read this as a pass, and never as a red test. |
