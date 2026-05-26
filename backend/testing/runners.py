"""
Test-runner adapters and auto-detection.

Supports unittest (default), pytest, jest, and vitest. Each runner returns a
`TestRunResult` so callers (CLI / agent runtime) can render uniformly.

Previously this lived in `backend/mcp_testing.py`; extracted so the runners
remain after the MCP server scaffolding is removed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from io import StringIO
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


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
    """Run a test command without inheriting the caller's stdio."""
    return subprocess.run(
        cmd,
        cwd=workspace_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Unittest
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
        if _is_frozen():
            suite = _discover_unittest_suite(workspace_path)
            return sorted(str(item) for item in _iter_unittest_cases(suite))
        r = _run_command(
            [sys.executable, "-c", _UNITTEST_LIST_SCRIPT],
            workspace_path=workspace_path,
            timeout=30,
        )
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]

    def run_all(self, workspace_path: str) -> TestRunResult:
        if _is_frozen():
            suite = _discover_unittest_suite(workspace_path)
            return _run_unittest_suite(suite)
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
        if _is_frozen():
            pattern = name_substr.lower()
            suite = _discover_unittest_suite(workspace_path)
            matched = unittest.TestSuite(
                item for item in _iter_unittest_cases(suite)
                if pattern in str(item).lower()
            )
            if matched.countTestCases() == 0:
                msg = f"No tests found matching '{name_substr}'"
                return TestRunResult(
                    passed=0,
                    failed=1,
                    results=[TestResult(name=name_substr, passed=False, error=msg)],
                    raw_output=msg,
                )
            return _run_unittest_suite(matched)
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


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _discover_unittest_suite(workspace_path: str) -> unittest.TestSuite:
    start_dir = Path(workspace_path) / "backend" / "tests"
    if not start_dir.exists():
        return unittest.TestSuite()
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
            passed=total - failed,
            failed=failed,
            results=[],
            raw_output=raw,
        )
    result.raw_output = raw
    return result


# ---------------------------------------------------------------------------
# Pytest
# ---------------------------------------------------------------------------


def _parse_pytest_output(raw: str) -> TestRunResult:
    lines = raw.splitlines()
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
# Jest
# ---------------------------------------------------------------------------


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
# Runner registry & auto-detection
# ---------------------------------------------------------------------------

RUNNERS: dict[str, type] = {
    "unittest": UnittestRunner,
    "pytest": PytestRunner,
    "jest": JestRunner,
    "vitest": VitestRunner,
}


def detect_runner(workspace_path: str) -> TestRunner:
    """Auto-detect the appropriate test runner.

    Detection order: pytest → jest → vitest → unittest (fallback).
    """
    root = Path(workspace_path)

    for marker in ("pytest.ini", "conftest.py"):
        if (root / marker).exists():
            return PytestRunner()
    setup_cfg = root / "setup.cfg"
    if setup_cfg.exists() and "[tool:pytest]" in setup_cfg.read_text(encoding="utf-8", errors="ignore"):
        return PytestRunner()
    pyproject = root / "pyproject.toml"
    if pyproject.exists() and "tool.pytest" in pyproject.read_text(encoding="utf-8", errors="ignore"):
        return PytestRunner()

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

    return UnittestRunner()


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
