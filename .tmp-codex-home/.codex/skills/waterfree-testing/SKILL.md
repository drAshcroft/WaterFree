---
name: waterfree-testing
description: Use the WaterFree test MCP tools to run tests, inspect failures, and retrieve logs — without reading raw terminal output.
---

# WaterFree — Test Runner MCP Tools

Provides a clean interface for running tests in any supported framework.
Auto-detects the framework from the project (pytest → jest → vitest → unittest).

## When to Use

Use these tools when you need to:
- Verify that existing tests still pass after a change — use `run_tests`
- Run a specific test to confirm a fix — use `run_test`
- See which tests exist before running one — use `list_tests`
- Read the full output of the last test run — use `get_test_logs`

## Tools

### `mcp__waterfree-testing__run_tests`

Run the full test suite and get a one-line summary.

```
run_tests(workspace_path: str)
-> "Yes: 42"                              # all pass
-> "Failing tests: 3/42\ntest_a\ntest_b" # failures with titles
```

### `mcp__waterfree-testing__run_test`

Run a single test (or all tests whose name contains the given substring).
Case-insensitive, substring match.

```
run_test(workspace_path: str, test_name: str)
-> "Yes"
-> "No: AssertionError: expected 1 but got 2 ..."
```

Use `list_tests` first if you are unsure of the exact test name.

### `mcp__waterfree-testing__get_test_logs`

Return the full stdout + stderr from the last `run_tests` or `run_test` call.

```
get_test_logs(workspace_path: str)
-> <raw test output>
```

Call this after a failure to see the complete traceback.

### `mcp__waterfree-testing__list_tests`

Discover and list all test names in the workspace.

```
list_tests(workspace_path: str)
-> ["test_add_task (test_todo_store.TaskStoreTests...)", ...]
```

## Recommended pattern

```
1. run_tests(workspace_path)            # Quick pass/fail summary
2.   -> if failing: get_test_logs(...)  # Full traceback
3.   -> fix code
4. run_test(workspace_path, "test_foo") # Confirm specific test passes
5. run_tests(workspace_path)            # Verify nothing else broke
```

## Supported frameworks

| Framework | Auto-detected by |
|-----------|-----------------|
| pytest | `pytest.ini`, `conftest.py`, `[tool.pytest]` in pyproject.toml |
| Jest | `jest.config.*`, `"jest"` in package.json |
| Vitest | `vitest.config.*`, `"vitest"` in package.json |
| unittest | fallback (default for WaterFree itself) |

## workspace_path

Always pass the absolute path to the project root, e.g. `c:\Projects\MyApp`.
Test logs are stored at `{workspace_path}\.waterfree\testing\last_run.log`.
