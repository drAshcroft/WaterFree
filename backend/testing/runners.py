"""
Test-runner adapters and auto-detection.

Supports unittest (default), pytest, jest, and vitest. Each runner returns a
`TestRunResult` so callers (CLI / agent runtime) can render uniformly.

Previously this lived in `backend/mcp_testing.py`; extracted so the runners
remain after the MCP server scaffolding is removed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Protocol

from backend.testing import dotnet as _dotnet
from backend.testing import godot as _godot
from backend.testing import npm_scripts as _npm_scripts
from backend.testing.bounded import RunLimits
from backend.testing.dotnet import (  # noqa: F401 -- re-exported
    DotnetRunner,
    is_dotnet_project,
)
from backend.testing.errors import (
    NoFrameworkError,
    NoTestsDiscoveredError,
    NoTestsMatchedError,
)
from backend.testing.godot import (  # noqa: F401 -- re-exported
    GodotRunner,
    is_godot_project,
)
from backend.testing.godot_script import GodotScriptRunner
from backend.testing.npm_scripts import NpmScriptRunner, is_npm_scripts_project
from backend.testing.playwright_e2e import PlaywrightRunner, is_playwright_project

# Re-exported: these used to be defined here, and callers import them from this
# module. They now live in `results` so framework adapters can share them
# without importing each other.
from backend.testing.results import TestResult, TestRunResult
from backend.testing.toolchain import check_filter_arg, node_tool, run_tool

__all__ = [
    "TestResult",
    "TestRunResult",
    "TestRunner",
    "UnittestRunner",
    "PytestRunner",
    "JestRunner",
    "VitestRunner",
    "GodotRunner",
    "GodotScriptRunner",
    "NpmScriptRunner",
    "DotnetRunner",
    "PlaywrightRunner",
    "RUNNERS",
    "DetectedRunner",
    "detect_runner",
    "detect_all_runners",
    "log_dir",
    "write_log",
    "read_log",
]


class TestRunner(Protocol):
    # The RUNNERS key, used in messages and in `--runner X` retry hints.
    name: str
    # --budget / --stall for this invocation; None means the runner's defaults.
    limits: RunLimits | None

    def run_all(self, workspace_path: str) -> TestRunResult: ...
    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult: ...
    def list_tests(self, workspace_path: str) -> list[str]: ...


def _run_command(
    cmd: list[str],
    *,
    workspace_path: str,
    timeout: int,
    limits: RunLimits | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a test command without inheriting the caller's stdio.

    Goes through `run_tool` so a command that cannot be spawned surfaces as a
    ToolchainError with install advice, not a bare OSError traceback, and so
    --budget / --stall and the process-tree kill apply.
    """
    return run_tool(cmd, workspace_path=workspace_path, timeout=timeout, limits=limits)


# ---------------------------------------------------------------------------
# Unittest
# ---------------------------------------------------------------------------

# Conventional locations for a Python unittest suite, most specific first.
# `backend/tests` is WaterFree's own layout and used to be hard-coded here,
# which meant the runner could be *selected* for a project that keeps its tests
# in `tests/` and then discover nothing in it.
UNITTEST_DIR_CANDIDATES = ("backend/tests", "tests", "test")


def unittest_start_dir(workspace_path: str) -> str | None:
    """The directory `unittest discover` should start from, or None.

    A candidate only counts when it actually contains `test_*.py`; an empty
    `tests/` directory is not a suite.
    """
    root = Path(workspace_path)
    for relative in UNITTEST_DIR_CANDIDATES:
        candidate = root.joinpath(*relative.split("/"))
        if candidate.is_dir() and any(candidate.rglob("test_*.py")):
            return relative
    return None


_UNITTEST_LIST_SCRIPT = """\
import unittest, sys
loader = unittest.TestLoader()
suite = loader.discover(start_dir=sys.argv[1], pattern='test_*.py')
def _names(s):
    for item in s:
        if hasattr(item, '__iter__'):
            yield from _names(item)
        else:
            yield str(item)
for name in sorted(_names(suite)):
    print(name)
"""

# Exit code + marker the run-one script uses for "the filter matched nothing".
# Both are required, so a suite that happens to exit 5 is not misread.
_UNITTEST_NO_MATCH_EXIT = 5
_UNITTEST_NO_MATCH_RE = re.compile(r"WATERFREE_NO_MATCH total=(\d+)")

_UNITTEST_RUN_ONE_SCRIPT = """\
import unittest, sys

start_dir = sys.argv[1]
pattern = sys.argv[2].lower()

def _items(s):
    for item in s:
        if hasattr(item, '__iter__'):
            yield from _items(item)
        else:
            yield item

loader = unittest.TestLoader()
suite = loader.discover(start_dir=start_dir, pattern='test_*.py')
everything = list(_items(suite))
matched = unittest.TestSuite(t for t in everything if pattern in str(t).lower())

if matched.countTestCases() == 0:
    print(f"No tests found matching '{sys.argv[2]}'", file=sys.stderr)
    print(f"WATERFREE_NO_MATCH total={len(everything)}", file=sys.stderr)
    sys.exit(5)

runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)
result = runner.run(matched)
sys.exit(0 if result.wasSuccessful() else 1)
"""


def _unittest_display_to_qualified(display: str) -> str:
    """Convert unittest display name to a qualified test id."""
    m = re.match(r"^(\w+)\s+\(([^)]+)\)$", display.strip())
    if m:
        method, rest = m.group(1), m.group(2)
        if rest.endswith(f".{method}"):
            return rest
        return f"{rest}.{method}"
    return display


def _parse_unittest_output(raw: str) -> TestRunResult:
    lines = raw.splitlines()
    results: list[TestResult] = []
    pending_name: str | None = None

    for line in lines:
        line_s = line.strip()
        m_inline = re.match(r"^(?P<name>.+?)\s+\.\.\.\s+(?P<status>ok|FAIL|ERROR|skipped.*)$", line_s)
        if m_inline:
            results.append(TestResult(
                name=m_inline.group("name").strip(),
                passed=m_inline.group("status").strip().lower() == "ok",
                error=None if m_inline.group("status").strip().lower() == "ok" else m_inline.group("status").strip(),
            ))
            pending_name = None
            continue

        m_pending = re.match(r"^(?P<name>.+?)\s+\.\.\.(?:\s+.+)?$", line_s)
        if m_pending:
            pending_name = m_pending.group("name").strip()
            continue

        if pending_name and re.match(r"^(ok|FAIL|ERROR|skipped.*)$", line_s):
            passed = line_s.lower() == "ok"
            results.append(TestResult(
                name=pending_name,
                passed=passed,
                error=None if passed else line_s,
            ))
            pending_name = None

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("FAIL: ") or stripped.startswith("ERROR: "):
            fail_display = stripped.split(": ", 1)[1].strip()
            i += 1
            while i < len(lines) and lines[i].startswith("-" * 10):
                i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].startswith("=" * 10) and not lines[i].startswith("-" * 10):
                block.append(lines[i])
                i += 1
            detail = "\n".join(block).strip()
            for r in results:
                if not r.passed and (fail_display in r.name or r.name in fail_display):
                    r.error = detail
                    break
        else:
            i += 1

    if not results:
        total = 0
        skipped_count = 0
        failed_count = 0
        for line in lines:
            m = re.match(r"^Ran (\d+) test", line)
            if m:
                total = int(m.group(1))
            skip = re.search(r"skipped=(\d+)", line)
            if skip:
                skipped_count = int(skip.group(1))
            m2 = re.search(r"FAILED \((.+)\)", line)
            if m2:
                groups = m2.group(1)
                f_m = re.search(r"failures=(\d+)", groups)
                e_m = re.search(r"errors=(\d+)", groups)
                failed_count = int(f_m.group(1) if f_m else 0) + int(e_m.group(1) if e_m else 0)
        return TestRunResult(
            passed=total - failed_count - skipped_count,
            skipped=skipped_count,
            failed=failed_count,
            results=[],
            raw_output=raw,
        )

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    return TestRunResult(passed=passed, failed=failed, results=results, raw_output=raw)


class UnittestRunner:
    name = "unittest"
    limits: RunLimits | None = None

    @staticmethod
    def _timeout() -> int:
        raw = os.environ.get("WATERFREE_UNITTEST_TIMEOUT", "900")
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("WATERFREE_UNITTEST_TIMEOUT must be a positive number of seconds") from exc
        if value <= 0:
            raise ValueError("WATERFREE_UNITTEST_TIMEOUT must be a positive number of seconds")
        return value

    def _run(self, cmd: list[str], workspace_path: str, *, name_filter: str | None = None) -> TestRunResult:
        timeout = self._timeout()
        try:
            r = _run_command(cmd, workspace_path=workspace_path, timeout=timeout, limits=self.limits)
        except subprocess.TimeoutExpired as exc:
            def text(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            raw = text(exc.stdout) + text(exc.stderr)
            message = f"Unittest runner exceeded {timeout}s. Set WATERFREE_UNITTEST_TIMEOUT above the suite's startup, build and cleanup deadlines. Inspect retained logs and any adapter-owned child processes."
            return TestRunResult(passed=0, failed=1,
                                 results=[TestResult(name="unittest timeout", passed=False, error=message)],
                                 raw_output=raw + '\n' + message)
        raw = r.stdout + r.stderr
        no_match = _UNITTEST_NO_MATCH_RE.search(raw)
        if name_filter is not None and r.returncode == _UNITTEST_NO_MATCH_EXIT and no_match:
            raise NoTestsMatchedError(
                runner=self.name,
                filter=name_filter,
                available=int(no_match.group(1)),
                raw_output=raw,
            )
        result = _parse_unittest_output(raw)
        result.raw_output = raw
        if r.returncode and not result.failed:
            result.failed = 1
            result.results.append(TestResult(name="unittest process", passed=False, error=f"Runner exited {r.returncode}: {raw}"))
        return result

    def list_tests(self, workspace_path: str) -> list[str]:
        if _is_frozen():
            suite = _discover_unittest_suite(workspace_path)
            return sorted(str(item) for item in _iter_unittest_cases(suite))
        r = _run_command(
            [sys.executable, "-c", _UNITTEST_LIST_SCRIPT, _start_dir(workspace_path)],
            workspace_path=workspace_path,
            timeout=30,
            limits=self.limits,
        )
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]

    def run_all(self, workspace_path: str) -> TestRunResult:
        if _is_frozen():
            suite = _discover_unittest_suite(workspace_path)
            return _run_unittest_suite(suite)
        return self._run(
            [sys.executable, "-m", "unittest", "discover",
             "-s", _start_dir(workspace_path), "-p", "test_*.py", "-v"],
            workspace_path,
        )

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        if _is_frozen():
            pattern = name_substr.lower()
            suite = _discover_unittest_suite(workspace_path)
            everything = list(_iter_unittest_cases(suite))
            matched = unittest.TestSuite(
                item for item in everything if pattern in str(item).lower()
            )
            if matched.countTestCases() == 0:
                raise NoTestsMatchedError(
                    runner=self.name,
                    filter=name_substr,
                    available=len(everything),
                    raw_output=f"No tests found matching '{name_substr}'",
                )
            return _run_unittest_suite(matched)
        return self._run(
            [sys.executable, "-c", _UNITTEST_RUN_ONE_SCRIPT,
             _start_dir(workspace_path), name_substr],
            workspace_path,
            name_filter=name_substr,
        )


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _start_dir(workspace_path: str) -> str:
    """Resolve the unittest start dir, or raise rather than search nothing."""
    relative = unittest_start_dir(workspace_path)
    if relative is None:
        raise NoTestsDiscoveredError(
            f"No Python unittest directory found under {workspace_path} "
            f"(looked for {', '.join(UNITTEST_DIR_CANDIDATES)} containing test_*.py)."
        )
    return relative


def _discover_unittest_suite(workspace_path: str) -> unittest.TestSuite:
    start_dir = Path(workspace_path) / _start_dir(workspace_path)
    loader = unittest.TestLoader()
    return loader.discover(start_dir=str(start_dir), pattern="test_*.py")


def _iter_unittest_cases(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_unittest_cases(item)
        else:
            yield item


def _run_unittest_suite(suite: unittest.TestSuite) -> TestRunResult:
    stream = StringIO()
    runner = unittest.TextTestRunner(verbosity=2, stream=stream)
    result_obj = runner.run(suite)
    raw = stream.getvalue()
    result = _parse_unittest_output(raw)
    if not result.results:
        total = result_obj.testsRun
        failed = len(result_obj.failures) + len(result_obj.errors)
        result = TestRunResult(
            passed=total - failed - len(result_obj.skipped),
            skipped=len(result_obj.skipped),
            failed=failed,
            results=[],
            raw_output=raw,
        )
    result.raw_output = raw
    return result


# ---------------------------------------------------------------------------
# Pytest
# ---------------------------------------------------------------------------


# `pytest -v` prints one line per test: `path::name PASSED   [ 50%]`. The short
# summary (`FAILED path::name - msg`) only lists failures, so parsing only that
# reported `passed: 0` for any run with a single failure in it.
_PYTEST_VERBOSE_RE = re.compile(
    r"^(?P<name>\S+::\S.*?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\(.*\))?(?:\s+\[\s*\d+%\])?\s*$"
)
_PYTEST_SUMMARY_RE = re.compile(r"^(?P<status>FAILED|ERROR) (?P<name>\S+::.+?)(?: - (?P<error>.*))?$")

# pytest's ExitCode.NO_TESTS_COLLECTED: nothing collected, or `-k` deselected all.
_PYTEST_NO_TESTS_EXIT = 5


def _parse_pytest_verbose(lines: list[str]) -> list[TestResult]:
    results: dict[str, TestResult] = {}
    for line in lines:
        match = _PYTEST_VERBOSE_RE.match(line.strip())
        if not match:
            continue
        name, status = match.group("name").strip(), match.group("status")
        if status in ("SKIPPED", "XFAIL"):
            results[name] = TestResult(name=name, passed=False, skipped=True, error="skipped")
        else:
            passed = status in ("PASSED", "XPASS")
            results[name] = TestResult(name=name, passed=passed, error=None if passed else status.lower())
    if not results:
        return []
    for line in lines:
        match = _PYTEST_SUMMARY_RE.match(line.strip())
        if match and match.group("name").strip() in results and match.group("error"):
            results[match.group("name").strip()].error = match.group("error")
    return list(results.values())


def _parse_pytest_output(raw: str) -> TestRunResult:
    lines = raw.splitlines()
    verbose = _parse_pytest_verbose(lines)
    if verbose:
        return TestRunResult(
            passed=sum(1 for r in verbose if r.passed),
            failed=sum(1 for r in verbose if not r.passed and not r.skipped),
            results=verbose,
            raw_output=raw,
        )

    results: list[TestResult] = []

    for line in lines:
        line_s = line.strip()
        if line_s.startswith("PASSED "):
            name = line_s[7:].strip()
            results.append(TestResult(name=name, passed=True))
        elif line_s.startswith("FAILED "):
            rest = line_s[7:]
            if " - " in rest:
                name_part, error_part = rest.split(" - ", 1)
            else:
                name_part, error_part = rest, None
            results.append(TestResult(name=name_part.strip(), passed=False, error=error_part))

    if not results:
        for line in lines:
            m_pass = re.search(r"(\d+) passed", line)
            m_fail = re.search(r"(\d+) failed", line)
            if m_pass or m_fail:
                p = int(m_pass.group(1)) if m_pass else 0
                f = int(m_fail.group(1)) if m_fail else 0
                return TestRunResult(passed=p, failed=f, results=[], raw_output=raw)

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    return TestRunResult(passed=passed, failed=failed, results=results, raw_output=raw)


class PytestRunner:
    name = "pytest"
    limits: RunLimits | None = None

    def list_tests(self, workspace_path: str) -> list[str]:
        r = _run_command(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
            workspace_path=workspace_path,
            timeout=30,
            limits=self.limits,
        )
        return [
            line.strip()
            for line in r.stdout.splitlines()
            if "::" in line and not line.strip().startswith("<")
        ]

    def run_all(self, workspace_path: str) -> TestRunResult:
        r = _run_command(
            [sys.executable, "-m", "pytest", "-v", "--tb=short", "--no-header"],
            workspace_path=workspace_path,
            timeout=120,
            limits=self.limits,
        )
        raw = r.stdout + r.stderr
        result = _parse_pytest_output(raw)
        result.raw_output = raw
        return result

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        r = _run_command(
            [sys.executable, "-m", "pytest", "-v", "--tb=short", "--no-header",
             "-k", name_substr],
            workspace_path=workspace_path,
            timeout=120,
            limits=self.limits,
        )
        raw = r.stdout + r.stderr
        lowered = raw.lower()
        if (r.returncode == _PYTEST_NO_TESTS_EXIT
                or "no tests ran" in lowered or "collected 0 items" in lowered):
            collected = re.search(r"collected (\d+) items?", raw)
            raise NoTestsMatchedError(
                runner=self.name,
                filter=name_substr,
                available=int(collected.group(1)) if collected else None,
                raw_output=raw,
            )
        result = _parse_pytest_output(raw)
        result.raw_output = raw
        return result


# ---------------------------------------------------------------------------
# Jest
# ---------------------------------------------------------------------------


_SKIPPED_STATUSES = frozenset({"skipped", "pending", "todo", "disabled"})


def _js_test_result(name: str, test: dict) -> TestResult:
    """Map one jest/vitest assertion result. Skipped-like statuses must not count as failures."""
    status = test.get("status", "")
    dur = test.get("duration")
    return TestResult(
        name=name,
        passed=status == "passed",
        error=chr(10).join(test.get("failureMessages", [])) or None,
        duration_ms=float(dur) if dur is not None else None,
        skipped=status in _SKIPPED_STATUSES,
    )


def _parse_jest_json(raw: str) -> TestRunResult:
    data: dict = {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        idx = raw.find("{")
        while idx != -1:
            try:
                data = json.loads(raw[idx:])
                break
            except json.JSONDecodeError:
                idx = raw.find("{", idx + 1)

    if not data:
        return TestRunResult(passed=0, failed=0, results=[], raw_output=raw)

    results: list[TestResult] = []
    for file_result in data.get("testResults", []):
        for test in file_result.get("testResults", []):
            name = test.get("fullName") or test.get("title", "unknown")
            results.append(_js_test_result(name, test))

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    return TestRunResult(passed=passed, failed=failed, results=results, raw_output=raw)


def _js_nothing_executed(runner: str, name_substr: str, result: TestRunResult, exit_code: int) -> NoTestsMatchedError:
    """The zero-match error for a filtered jest/vitest run.

    `-t` does not drop non-matching tests from the report, it marks them
    skipped/pending — so "known tests" is every result in the report, and a
    report with nothing in it at all is more likely a crash or a config error
    than a name mismatch. Say so rather than guess.
    """
    if result.results:
        return NoTestsMatchedError(
            runner=runner,
            filter=name_substr,
            available=len(result.results),
            skipped=result.skipped,
            raw_output=result.raw_output,
        )
    return NoTestsMatchedError(
        runner=runner,
        filter=name_substr,
        raw_output=result.raw_output,
        detail=(
            f"{runner} produced no parseable JSON report (exit code {exit_code}), "
            "which usually means it crashed or could not load its config rather "
            "than that the name is wrong — see `waterfree testing logs`."
        ),
    )


class JestRunner:
    name = "jest"
    limits: RunLimits | None = None

    def list_tests(self, workspace_path: str) -> list[str]:
        r = _run_command(
            node_tool(workspace_path, "jest") + ["--json", "--passWithNoTests"],
            workspace_path=workspace_path,
            timeout=60,
            limits=self.limits,
        )
        result = _parse_jest_json(r.stdout)
        return [t.name for t in result.results]

    def run_all(self, workspace_path: str) -> TestRunResult:
        r = _run_command(
            node_tool(workspace_path, "jest") + ["--json"],
            workspace_path=workspace_path,
            timeout=120,
            limits=self.limits,
        )
        result = _parse_jest_json(r.stdout)
        result.raw_output = r.stdout + r.stderr
        return result

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        check_filter_arg(name_substr)
        r = _run_command(
            node_tool(workspace_path, "jest") + ["-t", name_substr, "--json"],
            workspace_path=workspace_path,
            timeout=120,
            limits=self.limits,
        )
        result = _parse_jest_json(r.stdout)
        result.raw_output = r.stdout + r.stderr
        if result.passed + result.failed == 0:
            raise _js_nothing_executed(self.name, name_substr, result, r.returncode)
        return result


# ---------------------------------------------------------------------------
# Vitest
# ---------------------------------------------------------------------------


def _parse_vitest_json(raw: str) -> TestRunResult:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return TestRunResult(passed=0, failed=0, results=[], raw_output=raw)

    results: list[TestResult] = []
    for file_result in data.get("testResults", []):
        for test in file_result.get("assertionResults", []):
            ancestors = test.get("ancestorTitles", [])
            title = test.get("title", "")
            name = " > ".join(ancestors + [title]) if ancestors else title
            results.append(_js_test_result(name, test))

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    return TestRunResult(passed=passed, failed=failed, results=results, raw_output=raw)


class VitestRunner:
    name = "vitest"
    limits: RunLimits | None = None

    def _json_output_path(self, workspace_path: str) -> Path:
        p = Path(workspace_path) / ".waterfree" / "testing" / "_vitest_result.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def list_tests(self, workspace_path: str) -> list[str]:
        r = _run_command(
            node_tool(workspace_path, "vitest") + ["list"],
            workspace_path=workspace_path,
            timeout=30,
            limits=self.limits,
        )
        names: list[str] = []
        for line in r.stdout.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("✓") and not stripped.startswith("×") and "::" not in stripped:
                stripped = stripped.lstrip(">").strip()
                if stripped:
                    names.append(stripped)
        return names

    def run_all(self, workspace_path: str) -> TestRunResult:
        tmp = self._json_output_path(workspace_path)
        tmp.unlink(missing_ok=True)
        r = _run_command(
            node_tool(workspace_path, "vitest")
            + ["run", "--reporter=json", f"--outputFile={tmp}"],
            workspace_path=workspace_path,
            timeout=120,
            limits=self.limits,
        )
        if tmp.exists():
            raw = tmp.read_text(encoding="utf-8")
            tmp.unlink(missing_ok=True)
        else:
            raw = r.stdout
        result = _parse_vitest_json(raw)
        result.raw_output = raw + r.stderr
        return result

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        check_filter_arg(name_substr)
        tmp = self._json_output_path(workspace_path)
        # A report left behind by a run that was killed must not be read as
        # this run's result.
        tmp.unlink(missing_ok=True)
        r = _run_command(
            node_tool(workspace_path, "vitest")
            + ["run", "-t", name_substr, "--reporter=json", f"--outputFile={tmp}"],
            workspace_path=workspace_path,
            timeout=120,
            limits=self.limits,
        )
        if tmp.exists():
            raw = tmp.read_text(encoding="utf-8")
            tmp.unlink(missing_ok=True)
        else:
            raw = r.stdout
        result = _parse_vitest_json(raw)
        result.raw_output = raw + r.stderr
        if result.passed + result.failed == 0:
            raise _js_nothing_executed(self.name, name_substr, result, r.returncode)
        return result


# ---------------------------------------------------------------------------
# Runner registry & auto-detection
# ---------------------------------------------------------------------------

RUNNERS: dict[str, type] = {
    "unittest": UnittestRunner,
    "pytest": PytestRunner,
    "jest": JestRunner,
    "vitest": VitestRunner,
    "godot": GodotRunner,
    "godot-script": GodotScriptRunner,
    "npm-scripts": NpmScriptRunner,
    "dotnet": DotnetRunner,
    "playwright": PlaywrightRunner,
}


@dataclass(frozen=True)
class DetectedRunner:
    """One framework with positive evidence in a workspace.

    `auto` is False for opt-in runners (playwright, godot-script): they are
    reported so a dead end can name them, but detection never selects them.
    """

    name: str
    evidence: str
    auto: bool = True

    def create(self) -> TestRunner:
        return RUNNERS[self.name]()

    def to_dict(self) -> dict:
        payload = {"runner": self.name, "evidence": self.evidence}
        if not self.auto:
            payload["optIn"] = True
        return payload


def _has_unittest_layout(root: Path) -> bool:
    """True when there is a directory `unittest discover` would find tests in.

    Checked before falling back to the unittest runner. Without this, every
    workspace with no recognisable framework got a runner that discovers
    nothing and reports `0 passed, 0 failed` with exit 0, which every caller
    reads as a green suite. See NoFrameworkError.
    """
    return unittest_start_dir(str(root)) is not None


def _iter_detected(workspace_path: str):
    """Yield every runner with positive evidence, in detection-priority order.

    Order: godot → pytest → jest → vitest → unittest → npm scripts → dotnet,
    then the opt-in runners (playwright, godot-script). Most specific signal
    first. A generator so `detect_runner` stops at the first auto hit and pays
    for no more filesystem probing than it did before.
    """
    root = Path(workspace_path)

    # Godot goes first because it is the most specific — it requires both a
    # project.godot and an installed test-framework addon, so it never fires
    # on a project that merely happens to sit next to one.
    evidence = _godot.godot_evidence(workspace_path)
    if evidence:
        yield DetectedRunner("godot", evidence)

    evidence = _pytest_evidence(root)
    if evidence:
        yield DetectedRunner("pytest", evidence)

    # utf-8-sig: package.json is authored outside WaterFree and a BOM would
    # otherwise make framework detection silently fail.
    deps = _package_deps(root)
    evidence = _js_evidence(root, deps, "jest", _JEST_CONFIGS)
    if evidence:
        yield DetectedRunner("jest", evidence)
    evidence = _js_evidence(root, deps, "vitest", _VITEST_CONFIGS)
    if evidence:
        yield DetectedRunner("vitest", evidence)

    # Python before npm scripts. `test:*` scripts are the weakest signal we
    # accept: plenty of projects carry a couple of smoke scripts alongside a
    # real suite in another language. WaterFree itself is the example — it has
    # `test:acp` and `test:sidebar` next to 344 Python tests, and checking npm
    # first handed its own suite to the wrong runner.
    start_dir = unittest_start_dir(workspace_path)
    if start_dir is not None:
        yield DetectedRunner("unittest", f"{start_dir}/ with test_*.py")

    # No JS framework, but the project still ships test scripts (Paradoxia's 48
    # hand-rolled `tsx` suites). Better than pretending there are no tests.
    if is_npm_scripts_project(workspace_path):
        scripts = _npm_scripts.test_scripts(workspace_path)
        yield DetectedRunner("npm-scripts", "package.json scripts " + _sample(scripts))

    targets = _dotnet.find_solution_or_projects(workspace_path)
    if targets:
        yield DetectedRunner("dotnet", _sample([_relative(root, t) for t in targets]))

    # Opt-in only. Playwright: projects that have it almost always have a unit
    # suite too, and that is the faster, server-free default gate.
    if is_playwright_project(workspace_path):
        config = next(n for n in _PLAYWRIGHT_CONFIGS if (root / n).is_file())
        yield DetectedRunner("playwright", config, auto=False)

    # A SceneTree entry script (BrickWild's tests/run_all.gd) needs --script,
    # which detection cannot choose for you.
    scene_scripts = _godot.scene_tree_scripts(workspace_path)
    if scene_scripts:
        yield DetectedRunner(
            "godot-script", _sample(scene_scripts) + " (extends SceneTree)", auto=False,
        )


def detect_all_runners(workspace_path: str) -> list[DetectedRunner]:
    """Every runner with positive evidence here, in detection-priority order.

    Used to name the alternatives when the auto-picked runner matches nothing
    in a polyglot repo (a C# test name in a repo that also has Python tests).
    """
    return list(_iter_detected(workspace_path))


def detect_runner(workspace_path: str) -> TestRunner:
    """Auto-detect the appropriate test runner: the first auto candidate.

    See `_iter_detected` for the order. Playwright and godot-script are never
    auto-selected; ask for them with `--runner`.

    Raises NoFrameworkError when nothing matches, rather than returning a runner
    that is guaranteed to find nothing. Opt-in runners that do have evidence
    are named in the message with the flags that select them.
    """
    opt_in: list[DetectedRunner] = []
    for candidate in _iter_detected(workspace_path):
        if candidate.auto:
            return candidate.create()
        opt_in.append(candidate)

    message = (
        f"No supported test framework detected in {workspace_path}. Looked for: "
        "a Godot project with addons/gdUnit4 or addons/gut; pytest.ini / "
        "conftest.py / [tool.pytest]; a jest or vitest config or dependency; "
        "npm `test` / `test:*` scripts; a .NET solution or test project; or "
        "Python tests under tests/ or backend/tests/. "
        "Force one with --runner <name> if detection is wrong."
    )
    for candidate in opt_in:
        message += f" Found opt-in runner {candidate.name} ({candidate.evidence})"
        if candidate.name == "godot-script":
            first = _godot.scene_tree_scripts(workspace_path)[0]
            message += f": run it with --runner godot-script --script {first}"
        else:
            message += f": select it with --runner {candidate.name}"
        message += "."
    raise NoFrameworkError(message)


def runner_name(runner) -> str:
    """The RUNNERS key for a runner instance, for messages and retry hints."""
    name = getattr(runner, "name", None)
    if isinstance(name, str) and name:
        return name
    for key, cls in RUNNERS.items():
        if isinstance(runner, cls):
            return key
    return type(runner).__name__.replace("Runner", "").lower() or "runner"


def other_runners_for(workspace_path: str, chosen: str, name_filter: str) -> list[dict]:
    """The other runners a zero-match run-one could have meant.

    Each entry carries its evidence and a ready-made retry command. Where the
    runner can list its tests without booting anything (Godot and .NET read
    sources, npm scripts read package.json) the entry also says how many of its
    tests match the filter, so the hint is decisive rather than a guess.
    Runners with matches sort first; opt-in runners last.
    """
    entries: list[dict] = []
    for candidate in _iter_detected(workspace_path):
        if candidate.name == chosen:
            continue
        entry = candidate.to_dict()
        matches = _static_match_count(candidate.name, workspace_path, name_filter)
        if matches is not None:
            entry["matches"] = matches
        retry = f"waterfree testing run-one {_shell_quote(name_filter)} --runner {candidate.name}"
        if candidate.name == "godot-script":
            retry += " --script <res://path/to/script.gd>"
        entry["retry"] = retry
        entries.append(entry)
    entries.sort(key=lambda e: (-(e.get("matches") or 0), bool(e.get("optIn"))))
    return entries


def _static_match_count(name: str, workspace_path: str, name_filter: str) -> int | None:
    pattern = name_filter.lower()
    try:
        if name == "dotnet":
            names = _dotnet.scan_test_names(workspace_path)
        elif name == "godot":
            names = _godot.scan_test_names(_godot.find_godot_project(workspace_path))
        elif name == "npm-scripts":
            names = _npm_scripts.test_scripts(workspace_path)
        else:
            return None
    except (OSError, _godot.GodotError):
        return None
    return sum(1 for n in names if pattern in n.lower())


_JEST_CONFIGS = ("jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs")
_VITEST_CONFIGS = ("vitest.config.ts", "vitest.config.js", "vitest.config.mts")
_PLAYWRIGHT_CONFIGS = (
    "playwright.config.ts", "playwright.config.js", "playwright.config.mjs", "playwright.config.cjs",
)


def _pytest_evidence(root: Path) -> str | None:
    for marker in ("pytest.ini", "conftest.py"):
        if (root / marker).exists():
            return marker
    setup_cfg = root / "setup.cfg"
    if setup_cfg.exists() and "[tool:pytest]" in setup_cfg.read_text(encoding="utf-8", errors="ignore"):
        return "setup.cfg [tool:pytest]"
    pyproject = root / "pyproject.toml"
    if pyproject.exists() and "tool.pytest" in pyproject.read_text(encoding="utf-8", errors="ignore"):
        return "pyproject.toml [tool.pytest]"
    return None


def _js_evidence(root: Path, deps: dict, package: str, configs: tuple[str, ...]) -> str | None:
    # Config file before dependency. Jest is yielded before vitest, so a jest
    # config or dependency still outranks vitest, as it always has.
    for marker in configs:
        if (root / marker).exists():
            return marker
    if package in deps:
        return f"package.json dependency {package}"
    return None


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _sample(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown += f" (+{len(items) - limit} more)"
    return shown


def _shell_quote(value: str) -> str:
    """Quote a filter for a copy-pasteable retry command.

    Single quotes work in bash and PowerShell; an embedded apostrophe is doubled,
    which is PowerShell syntax. Test names with apostrophes are rare enough that
    one dialect is the right trade.
    """
    if re.fullmatch(r"[\w.:/=+-]+", value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _package_deps(root: Path) -> dict:
    """dependencies + devDependencies from package.json, or {} when absent."""
    pkg_json = root / "package.json"
    if not pkg_json.exists():
        return {}
    try:
        pkg = json.loads(pkg_json.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(pkg, dict):
        return {}
    return {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------


def log_dir(workspace_path: str) -> Path:
    d = Path(workspace_path) / ".waterfree" / "testing"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_log(workspace_path: str, content: str) -> None:
    (log_dir(workspace_path) / "last_run.log").write_text(content, encoding="utf-8")


def read_log(workspace_path: str) -> str:
    log_file = log_dir(workspace_path) / "last_run.log"
    if not log_file.exists():
        return "No test logs found. Run tests first."
    return log_file.read_text(encoding="utf-8")
