"""
MCP server — test runner tools.

Exposes test running capabilities as MCP tools so Claude Code and other MCP
clients can run tests, check results, and retrieve logs without dealing with
raw terminal output.

Supported frameworks (auto-detected):
  - Python unittest (default)
  - pytest
  - Jest
  - Vitest

Run:
    python -m backend.mcp_testing

Register with Claude Code:
    claude mcp add waterfree-testing python -- -m backend.mcp_testing
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mcp.server.fastmcp import FastMCP

from backend.mcp_logging import configure_mcp_logger, instrument_tool

mcp = FastMCP("waterfree-testing")
log, LOG_FILE = configure_mcp_logger("waterfree-testing")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class TestResult:
    name: str
    passed: bool
    error: str | None = None
    duration_ms: float | None = None


@dataclass
class TestRunResult:
    passed: int
    failed: int
    results: list[TestResult] = field(default_factory=list)
    raw_output: str = ""


# ---------------------------------------------------------------------------
# Runner Protocol
# ---------------------------------------------------------------------------


class TestRunner(Protocol):
    def run_all(self, workspace_path: str) -> TestRunResult: ...
    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult: ...
    def list_tests(self, workspace_path: str) -> list[str]: ...


def _run_command(
    cmd: list[str],
    *,
    workspace_path: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run a test command without inheriting the MCP stdio pipe."""
    return subprocess.run(
        cmd,
        cwd=workspace_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Unittest Runner
# ---------------------------------------------------------------------------

_UNITTEST_LIST_SCRIPT = """\
import unittest
loader = unittest.TestLoader()
suite = loader.discover(start_dir='backend/tests', pattern='test_*.py')
def _names(s):
    for item in s:
        if hasattr(item, '__iter__'):
            yield from _names(item)
        else:
            yield str(item)
for name in sorted(_names(suite)):
    print(name)
"""

# Discovers tests, filters by substring, then runs matching ones in-process.
_UNITTEST_RUN_ONE_SCRIPT = """\
import unittest, sys

pattern = sys.argv[1].lower()

def _items(s):
    for item in s:
        if hasattr(item, '__iter__'):
            yield from _items(item)
        else:
            yield item

loader = unittest.TestLoader()
suite = loader.discover(start_dir='backend/tests', pattern='test_*.py')
matched = unittest.TestSuite(t for t in _items(suite) if pattern in str(t).lower())

if matched.countTestCases() == 0:
    print(f"No tests found matching '{sys.argv[1]}'", file=sys.stderr)
    sys.exit(2)

runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)
result = runner.run(matched)
sys.exit(0 if result.wasSuccessful() else 1)
"""


def _unittest_display_to_qualified(display: str) -> str:
    """Convert unittest display name to a qualified test id.

    Handles both formats:
      Python <3.12:  'test_foo (pkg.module.ClassName)'
      Python 3.12+:  'test_foo (pkg.module.ClassName.test_foo)'
    """
    m = re.match(r"^(\w+)\s+\(([^)]+)\)$", display.strip())
    if m:
        method, rest = m.group(1), m.group(2)
        # Python 3.12+: parenthesised part already ends with the method name
        if rest.endswith(f".{method}"):
            return rest
        return f"{rest}.{method}"
    return display


def _parse_unittest_output(raw: str) -> TestRunResult:
    lines = raw.splitlines()
    results: list[TestResult] = []
    pending_name: str | None = None

    # Parse verbose unittest output, including cases where logs are emitted
    # between "..."" and the final status line.
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

    # Attach detailed error/failure messages to failing results
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("FAIL: ") or stripped.startswith("ERROR: "):
            fail_display = stripped.split(": ", 1)[1].strip()
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

    # Fallback: parse summary when no verbose per-test lines were found
    if not results:
        total = 0
        failed_count = 0
        for line in lines:
            m = re.match(r"^Ran (\d+) test", line)
            if m:
                total = int(m.group(1))
            m2 = re.search(r"FAILED \((.+)\)", line)
            if m2:
                groups = m2.group(1)
                f_m = re.search(r"failures=(\d+)", groups)
                e_m = re.search(r"errors=(\d+)", groups)
                failed_count = int(f_m.group(1) if f_m else 0) + int(e_m.group(1) if e_m else 0)
        return TestRunResult(
            passed=total - failed_count,
            failed=failed_count,
            results=[],
            raw_output=raw,
        )

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    return TestRunResult(passed=passed, failed=failed, results=results, raw_output=raw)


class UnittestRunner:
    def list_tests(self, workspace_path: str) -> list[str]:
        r = _run_command(
            [sys.executable, "-c", _UNITTEST_LIST_SCRIPT],
            workspace_path=workspace_path,
            timeout=30,
        )
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]

    def run_all(self, workspace_path: str) -> TestRunResult:
        r = _run_command(
            [sys.executable, "-m", "unittest", "discover",
             "-s", "backend/tests", "-p", "test_*.py", "-v"],
            workspace_path=workspace_path,
            timeout=120,
        )
        raw = r.stdout + r.stderr
        result = _parse_unittest_output(raw)
        result.raw_output = raw
        return result

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        r = _run_command(
            [sys.executable, "-c", _UNITTEST_RUN_ONE_SCRIPT, name_substr],
            workspace_path=workspace_path,
            timeout=120,
        )
        raw = r.stdout + r.stderr
        if r.returncode == 2:
            msg = f"No tests found matching '{name_substr}'"
            return TestRunResult(
                passed=0, failed=1,
                results=[TestResult(name=name_substr, passed=False, error=msg)],
                raw_output=raw,
            )
        result = _parse_unittest_output(raw)
        result.raw_output = raw
        return result


# ---------------------------------------------------------------------------
# Pytest Runner
# ---------------------------------------------------------------------------


def _parse_pytest_output(raw: str) -> TestRunResult:
    lines = raw.splitlines()
    results: list[TestResult] = []

    for line in lines:
        line_s = line.strip()
        # Verbose mode lines: "PASSED path::Class::method" or "FAILED path::Class::method - msg"
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

    # Fallback: summary line "N passed, M failed in Xs"
    if not results:
        for line in lines:
            m_pass = re.search(r"(\d+) passed", line)
            m_fail = re.search(r"(\d+) failed", line)
            if m_pass or m_fail:
                p = int(m_pass.group(1)) if m_pass else 0
                f = int(m_fail.group(1)) if m_fail else 0
                return TestRunResult(passed=p, failed=f, results=[], raw_output=raw)

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    return TestRunResult(passed=passed, failed=failed, results=results, raw_output=raw)


class PytestRunner:
    def list_tests(self, workspace_path: str) -> list[str]:
        r = _run_command(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
            workspace_path=workspace_path,
            timeout=30,
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
        )
        raw = r.stdout + r.stderr
        if "no tests ran" in raw.lower() or "collected 0 items" in raw.lower():
            msg = f"No tests found matching '{name_substr}'"
            return TestRunResult(
                passed=0, failed=1,
                results=[TestResult(name=name_substr, passed=False, error=msg)],
                raw_output=raw,
            )
        result = _parse_pytest_output(raw)
        result.raw_output = raw
        return result


# ---------------------------------------------------------------------------
# Jest Runner
# ---------------------------------------------------------------------------


def _parse_jest_json(raw: str) -> TestRunResult:
    data: dict = {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Jest may prefix JSON with progress output; find the first '{' that parses
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
            passed = test.get("status", "") == "passed"
            error = "\n".join(test.get("failureMessages", [])) or None
            dur = test.get("duration")
            results.append(TestResult(
                name=name, passed=passed, error=error,
                duration_ms=float(dur) if dur is not None else None,
            ))

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    return TestRunResult(passed=passed, failed=failed, results=results, raw_output=raw)


class JestRunner:
    def list_tests(self, workspace_path: str) -> list[str]:
        r = _run_command(
            ["npx", "jest", "--json", "--passWithNoTests"],
            workspace_path=workspace_path,
            timeout=60,
        )
        result = _parse_jest_json(r.stdout)
        return [t.name for t in result.results]

    def run_all(self, workspace_path: str) -> TestRunResult:
        r = _run_command(
            ["npx", "jest", "--json"],
            workspace_path=workspace_path,
            timeout=120,
        )
        result = _parse_jest_json(r.stdout)
        result.raw_output = r.stdout + r.stderr
        return result

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        r = _run_command(
            ["npx", "jest", "-t", name_substr, "--json"],
            workspace_path=workspace_path,
            timeout=120,
        )
        result = _parse_jest_json(r.stdout)
        result.raw_output = r.stdout + r.stderr
        if not result.results:
            msg = f"No tests found matching '{name_substr}'"
            return TestRunResult(
                passed=0, failed=1,
                results=[TestResult(name=name_substr, passed=False, error=msg)],
                raw_output=result.raw_output,
            )
        return result


# ---------------------------------------------------------------------------
# Vitest Runner
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
            passed = test.get("status", "") == "passed"
            error = "\n".join(test.get("failureMessages", [])) or None
            dur = test.get("duration")
            results.append(TestResult(
                name=name, passed=passed, error=error,
                duration_ms=float(dur) if dur is not None else None,
            ))

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    return TestRunResult(passed=passed, failed=failed, results=results, raw_output=raw)


class VitestRunner:
    def _json_output_path(self, workspace_path: str) -> Path:
        p = Path(workspace_path) / ".waterfree" / "testing" / "_vitest_result.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def list_tests(self, workspace_path: str) -> list[str]:
        r = _run_command(
            ["npx", "vitest", "list"],
            workspace_path=workspace_path,
            timeout=30,
        )
        # vitest list outputs one test name per line, prefixed with checkmarks or file paths
        names: list[str] = []
        for line in r.stdout.splitlines():
            stripped = line.strip()
            # Skip file path lines and empty lines
            if stripped and not stripped.startswith("✓") and not stripped.startswith("×") and "::" not in stripped:
                # Remove leading ">" markers
                stripped = stripped.lstrip(">").strip()
                if stripped:
                    names.append(stripped)
        return names

    def run_all(self, workspace_path: str) -> TestRunResult:
        tmp = self._json_output_path(workspace_path)
        r = _run_command(
            ["npx", "vitest", "run", "--reporter=json", f"--outputFile={tmp}"],
            workspace_path=workspace_path,
            timeout=120,
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
        tmp = self._json_output_path(workspace_path)
        r = _run_command(
            ["npx", "vitest", "run", "-t", name_substr,
             "--reporter=json", f"--outputFile={tmp}"],
            workspace_path=workspace_path,
            timeout=120,
        )
        if tmp.exists():
            raw = tmp.read_text(encoding="utf-8")
            tmp.unlink(missing_ok=True)
        else:
            raw = r.stdout
        result = _parse_vitest_json(raw)
        result.raw_output = raw + r.stderr
        if not result.results:
            msg = f"No tests found matching '{name_substr}'"
            return TestRunResult(
                passed=0, failed=1,
                results=[TestResult(name=name_substr, passed=False, error=msg)],
                raw_output=result.raw_output,
            )
        return result


# ---------------------------------------------------------------------------
# Runner Registry & Auto-detection
# ---------------------------------------------------------------------------

RUNNERS: dict[str, type] = {
    "unittest": UnittestRunner,
    "pytest": PytestRunner,
    "jest": JestRunner,
    "vitest": VitestRunner,
}


def detect_runner(workspace_path: str) -> TestRunner:
    """Auto-detect the appropriate test runner for the given workspace.

    Detection order: pytest → jest → vitest → unittest (fallback).
    """
    root = Path(workspace_path)

    # Pytest markers
    for marker in ("pytest.ini", "conftest.py"):
        if (root / marker).exists():
            return PytestRunner()
    setup_cfg = root / "setup.cfg"
    if setup_cfg.exists() and "[tool:pytest]" in setup_cfg.read_text(encoding="utf-8", errors="ignore"):
        return PytestRunner()
    pyproject = root / "pyproject.toml"
    if pyproject.exists() and "tool.pytest" in pyproject.read_text(encoding="utf-8", errors="ignore"):
        return PytestRunner()

    # Jest markers
    for marker in ("jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs"):
        if (root / marker).exists():
            return JestRunner()
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}
            if "jest" in deps:
                return JestRunner()
        except json.JSONDecodeError:
            pass

    # Vitest markers
    for marker in ("vitest.config.ts", "vitest.config.js", "vitest.config.mts"):
        if (root / marker).exists():
            return VitestRunner()
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}
            if "vitest" in deps:
                return VitestRunner()
        except json.JSONDecodeError:
            pass

    # Default: unittest
    return UnittestRunner()


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------


def _log_dir(workspace_path: str) -> Path:
    d = Path(workspace_path) / ".waterfree" / "testing"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_log(workspace_path: str, content: str) -> None:
    (_log_dir(workspace_path) / "last_run.log").write_text(content, encoding="utf-8")


def _read_log(workspace_path: str) -> str:
    log_file = _log_dir(workspace_path) / "last_run.log"
    if not log_file.exists():
        return "No test logs found. Run tests first."
    return log_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# MCP Tool Implementations
# ---------------------------------------------------------------------------


def _run_tests_impl(workspace_path: str) -> str:
    """Run all tests in the workspace and return a concise summary.

    Args:
        workspace_path: Absolute path to the project root.

    Returns 'Yes: N' if all N tests pass, or
    'Failing tests: F/T\\ntitle1\\ntitle2' listing failing test titles.
    Use get_test_logs to see the full output.
    """
    runner = detect_runner(workspace_path)
    result = runner.run_all(workspace_path)
    _write_log(workspace_path, result.raw_output)
    total = result.passed + result.failed
    if result.failed == 0:
        return f"Yes: {result.passed}"
    failing_titles = [r.name for r in result.results if not r.passed]
    titles_str = "\n".join(failing_titles) if failing_titles else "(see logs)"
    return f"Failing tests: {result.failed}/{total}\n{titles_str}"


def _run_test_impl(workspace_path: str, test_name: str) -> str:
    """Run a specific test by name (substring match, case-insensitive).

    Args:
        workspace_path: Absolute path to the project root.
        test_name: Substring to match against test names.
                   Use list_tests to discover available names.

    Returns 'Yes' if all matching tests pass, or 'No: error message' on failure.
    Use get_test_logs to see the full output.
    """
    runner = detect_runner(workspace_path)
    result = runner.run_one(workspace_path, test_name)
    _write_log(workspace_path, result.raw_output)
    if result.failed == 0 and result.passed > 0:
        return "Yes"
    if result.failed == 0 and result.passed == 0:
        return f"No: No tests found matching '{test_name}'"
    for r in result.results:
        if not r.passed and r.error:
            msg = r.error[:500] + ("..." if len(r.error) > 500 else "")
            return f"No: {msg}"
    return f"No: {result.failed} test(s) failed (use get_test_logs for details)"


def _get_test_logs_impl(workspace_path: str) -> str:
    """Return the raw output from the last test run.

    Args:
        workspace_path: Absolute path to the project root.

    Returns the full stdout+stderr from the most recent run_tests or run_test call.
    """
    return _read_log(workspace_path)


def _list_tests_impl(workspace_path: str) -> str:
    """Discover and list all available test names in the workspace.

    Args:
        workspace_path: Absolute path to the project root.

    Returns a JSON array of test name strings.
    Pass any of these (or a substring) to run_test.
    """
    runner = detect_runner(workspace_path)
    names = runner.list_tests(workspace_path)
    return json.dumps(names, indent=2)


# ---------------------------------------------------------------------------
# Register tools with MCP
# ---------------------------------------------------------------------------

run_tests = mcp.tool()(instrument_tool(log, "run_tests", _run_tests_impl))
run_test = mcp.tool()(instrument_tool(log, "run_test", _run_test_impl))
get_test_logs = mcp.tool()(instrument_tool(log, "get_test_logs", _get_test_logs_impl))
list_tests = mcp.tool()(instrument_tool(log, "list_tests", _list_tests_impl))


if __name__ == "__main__":
    log.info("Starting MCP server waterfree-testing (logFile=%s)", LOG_FILE)
    mcp.run()
