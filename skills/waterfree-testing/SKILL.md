---
name: waterfree-testing
description: Use the `waterfree testing` CLI to run tests, inspect failures, and retrieve logs — without reading raw terminal output.
---

# WaterFree — Test Runner

Provides a clean interface for running tests in any supported framework via the
`waterfree` CLI. Auto-detects the framework from the project
(pytest → jest → vitest → unittest).

Each invocation is a short shell command — run it through Bash. All commands
emit JSON to stdout (except `logs`, which prints raw test output).

## When to Use

- Verify that existing tests still pass after a change — `waterfree testing run`
- Run a specific test to confirm a fix — `waterfree testing run-one <substr>`
- See which tests exist before running one — `waterfree testing list`
- Read the full output of the last test run — `waterfree testing logs`

## CLI

All commands accept `--workspace <path>` (defaults to CWD).

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

## Recommended pattern

```bash
waterfree testing run --workspace .                  # Quick pass/fail summary
# if failing:
waterfree testing logs --workspace .                 # Full traceback
# fix code, then:
waterfree testing run-one "test_foo" --workspace .   # Confirm specific test passes
waterfree testing run --workspace .                  # Verify nothing else broke
```

## Supported frameworks

| Framework | Auto-detected by |
|-----------|-----------------|
| pytest    | `pytest.ini`, `conftest.py`, `[tool.pytest]` in pyproject.toml |
| Jest      | `jest.config.*`, `"jest"` in package.json |
| Vitest    | `vitest.config.*`, `"vitest"` in package.json |
| unittest  | fallback (default for WaterFree itself) |

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
