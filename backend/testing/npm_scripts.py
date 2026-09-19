"""
Runner for projects whose tests are plain npm scripts.

Not every JS project uses a framework we can drive directly. Paradoxia, for
example, has 48 `test:*` scripts that each run one hand-rolled `tsx` suite, and
a `test` script that chains them with `&&`. Auto-detection used to fall through
such a project to the unittest runner, discover nothing, and report
`0 passed, 0 failed, exit 0` — a green light for a suite that had never run.

Strategy:

* Each `test:*` script is one unit. They are run individually and in parallel,
  because the `&&` chain stops at the first failure and would hide the rest —
  and because 48 sequential `tsx` boots is about seven minutes.
* A bare `test` script with no `test:*` siblings is run as a single unit.
* Per-test detail is recovered from the output when the suite prints a
  recognisable tick/cross line. Hand-rolled harnesses vary, so parsing is
  best-effort and the exit code is always authoritative: a script that exits
  non-zero is failed even if every line we parsed looked like a pass.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.testing.errors import NoTestsDiscoveredError
from backend.testing.results import TestResult, TestRunResult
from backend.testing.toolchain import check_filter_arg, npm, run_tool

DEFAULT_TIMEOUT_SECONDS = 300

# Bounded because each script is a separate node boot; too many at once just
# thrashes. Overridable for slow machines and very large suites.
DEFAULT_WORKERS = 6

# Frameworks that have their own adapter. A project carrying one of these is
# not an npm-scripts project, even if it also has test:* scripts.
FRAMEWORK_DEPS = ("jest", "vitest", "mocha", "@playwright/test")

_PASS_LINE = re.compile(r"^\s*(?:✓|✔|√|PASS|ok\s+\d+)\s+[-–]?\s*(?P<name>\S.*?)\s*$")
# No bare "x" marker: too many ordinary log lines begin with one, and a false
# failure is more expensive here than a missed one (the exit-code check below
# catches anything this misses).
_FAIL_LINE = re.compile(
    r"^\s*(?:✗|✘|×|FAIL|not ok\s+\d+)\s+[-–]?\s*(?P<name>\S.*?)\s*$"
)
# Lines a harness prints *about* the run rather than about one test. Without
# this the totals line ("15/15 tests passed, 0 failed") parses as a test named
# "tests passed, 0 failed".
_SUMMARY_LINE = re.compile(
    r"^\s*\d+\s*/\s*\d+\s+tests?\b|^\s*\d+\s+(?:passing|failing|passed|failed)\b",
    re.IGNORECASE,
)


def read_package_json(workspace_path: str) -> dict:
    """Parse the workspace package.json, or return {} when absent/unreadable.

    utf-8-sig: package.json is authored outside WaterFree and a BOM would
    otherwise make every lookup here silently fail.
    """
    path = Path(workspace_path) / "package.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def test_scripts(workspace_path: str) -> list[str]:
    """Names of the npm scripts that constitute this project's test suite.

    `test:*` scripts when there are any, otherwise a bare `test`. Scripts that
    only chain others (`npm run test:a && npm run test:b`) are dropped, so a
    suite is never counted twice.
    """
    scripts = read_package_json(workspace_path).get("scripts")
    if not isinstance(scripts, dict):
        return []

    specific = sorted(
        name for name, body in scripts.items()
        if name.startswith("test:") and isinstance(body, str) and not _is_aggregate(body)
    )
    if specific:
        return specific

    body = scripts.get("test")
    if isinstance(body, str) and body.strip() and not _is_aggregate(body):
        return ["test"]
    return []


def _is_aggregate(body: str) -> bool:
    """True when a script just chains other npm scripts."""
    parts = [p.strip() for p in re.split(r"&&|\|\|", body) if p.strip()]
    return len(parts) > 1 and all(p.startswith("npm run ") for p in parts)


def has_framework_dep(workspace_path: str) -> bool:
    """True when the project depends on a framework with a dedicated adapter."""
    pkg = read_package_json(workspace_path)
    deps = {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}
    return any(dep in deps for dep in FRAMEWORK_DEPS)


def is_npm_scripts_project(workspace_path: str) -> bool:
    """True when tests here are npm scripts and nothing more specific applies."""
    return bool(test_scripts(workspace_path)) and not has_framework_dep(workspace_path)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def parse_script_output(script: str, raw: str, *, exit_code: int) -> list[TestResult]:
    """Best-effort per-test results from one script's output.

    Returns [] when the harness printed nothing we recognise; the caller then
    falls back to a single result for the whole script.
    """
    results: list[TestResult] = []
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if _SUMMARY_LINE.match(line):
            continue
        match = _PASS_LINE.match(line)
        if match:
            results.append(TestResult(name=f"{script} > {match.group('name')}", passed=True))
            continue
        match = _FAIL_LINE.match(line)
        if match:
            results.append(TestResult(
                name=f"{script} > {match.group('name')}",
                passed=False,
                error=_failure_detail(lines, index),
            ))

    # The exit code is the ground truth. A harness that dies partway through, or
    # reports failures in a shape we did not parse, must not come back green.
    if exit_code != 0 and all(r.passed for r in results):
        results.append(TestResult(
            name=script,
            passed=False,
            error=f"`npm run {script}` exited with code {exit_code}.\n{_tail(raw)}",
        ))
    return results


def _failure_detail(lines: list[str], index: int) -> str:
    """The indented explanation a harness prints under a failing test."""
    detail: list[str] = []
    for line in lines[index + 1:]:
        if not line.strip():
            break
        if _PASS_LINE.match(line) or _FAIL_LINE.match(line):
            break
        detail.append(line.strip())
    return "\n".join(detail) or "failed"


def _tail(raw: str, limit: int = 40) -> str:
    lines = raw.strip().splitlines()
    return "\n".join(lines[-limit:])


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class NpmScriptRunner:
    """Runs each `test:*` npm script as one test unit."""

    def __init__(self, *, workers: int | None = None, timeout: int | None = None) -> None:
        self._workers = workers
        self._timeout = timeout

    # -- TestRunner protocol -------------------------------------------------

    def list_tests(self, workspace_path: str) -> list[str]:
        return test_scripts(workspace_path)

    def run_all(self, workspace_path: str) -> TestRunResult:
        return self._run(workspace_path, test_scripts(workspace_path))

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        check_filter_arg(name_substr)
        pattern = name_substr.lower()
        matched = [s for s in test_scripts(workspace_path) if pattern in s.lower()]
        if not matched:
            msg = f"No npm test script matching '{name_substr}'"
            return TestRunResult(
                passed=0, failed=1,
                results=[TestResult(name=name_substr, passed=False, error=msg)],
                raw_output=msg,
            )
        return self._run(workspace_path, matched)

    # -- internals -----------------------------------------------------------

    def _run(self, workspace_path: str, scripts: list[str]) -> TestRunResult:
        if not scripts:
            raise NoTestsDiscoveredError(
                f"No test scripts found in {Path(workspace_path) / 'package.json'}. "
                "Expected a `test` script or one or more `test:*` scripts."
            )

        npm_bin = npm(workspace_path)
        timeout = self._timeout or _timeout_seconds()
        workers = max(1, min(self._workers or _worker_count(), len(scripts)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outputs = list(pool.map(
                lambda script: self._run_script(npm_bin, workspace_path, script, timeout),
                scripts,
            ))

        results: list[TestResult] = []
        transcript: list[str] = []
        for script, exit_code, raw in outputs:
            transcript.append(f"$ npm run {script}\n{raw}")
            parsed = parse_script_output(script, raw, exit_code=exit_code)
            results.extend(parsed or [TestResult(
                name=script,
                passed=exit_code == 0,
                error=None if exit_code == 0
                else f"exited with code {exit_code}\n{_tail(raw)}",
            )])

        return TestRunResult(
            passed=sum(1 for r in results if r.passed),
            failed=sum(1 for r in results if not r.passed),
            results=results,
            raw_output="\n\n".join(transcript),
        )

    def _run_script(
        self, npm_bin: str, workspace_path: str, script: str, timeout: int,
    ) -> tuple[str, int, str]:
        try:
            proc = run_tool(
                [npm_bin, "run", "--silent", script],
                workspace_path=workspace_path,
                timeout=timeout,
                # Hand-rolled harnesses print ticks and box-drawing characters;
                # without this node picks the console codepage and the output we
                # parse arrives mojibaked on Windows.
                env={"FORCE_COLOR": "0", "NO_COLOR": "1"},
            )
        except subprocess.TimeoutExpired:
            return script, 124, f"`npm run {script}` timed out after {timeout}s."
        return script, proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _timeout_seconds() -> int:
    raw = os.environ.get("WATERFREE_NPM_TIMEOUT", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_TIMEOUT_SECONDS


def _worker_count() -> int:
    raw = os.environ.get("WATERFREE_NPM_WORKERS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_WORKERS
