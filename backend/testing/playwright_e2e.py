"""
Playwright end-to-end test support.

Kept deliberately off the auto-detection path. Projects that have Playwright
almost always have a unit-test framework too (goblinchess has both Vitest and
Playwright), and the unit suite is the gate you want by default: it is seconds
rather than minutes and needs no dev server. Select this one explicitly with
`--runner playwright`.

Results come from Playwright's JSON reporter, whose shape is a recursive tree
of suites; `--reporter=json` writes it to stdout unless PLAYWRIGHT_JSON_OUTPUT_NAME
is set, so we set that and read the file instead. Reading a file avoids the
long-standing problem of the report being interleaved with whatever the app
under test wrote to stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from backend.testing.errors import ToolchainError
from backend.testing.results import TestResult, TestRunResult
from backend.testing.toolchain import check_filter_arg, node_tool, run_tool

DEFAULT_TIMEOUT_SECONDS = 900

CONFIG_NAMES = (
    "playwright.config.ts",
    "playwright.config.js",
    "playwright.config.mjs",
    "playwright.config.cjs",
)


def is_playwright_project(workspace_path: str) -> bool:
    root = Path(workspace_path)
    return any((root / name).is_file() for name in CONFIG_NAMES)


def parse_playwright_json(text: str) -> list[TestResult]:
    """Flatten Playwright's recursive suite tree into TestResults."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    results: list[TestResult] = []
    for suite in data.get("suites", []):
        _walk_suite(suite, trail=[], out=results)
    return results


def _walk_suite(suite: dict, *, trail: list[str], out: list[TestResult]) -> None:
    title = suite.get("title") or ""
    here = trail + [title] if title else trail

    for spec in suite.get("specs", []):
        spec_title = spec.get("title") or "unknown"
        name = " > ".join(here + [spec_title])
        for test in spec.get("tests", []) or [{}]:
            project = test.get("projectName")
            label = f"[{project}] {name}" if project else name
            out.append(_spec_result(label, spec, test))

    for child in suite.get("suites", []):
        _walk_suite(child, trail=here, out=out)


def _spec_result(name: str, spec: dict, test: dict) -> TestResult:
    """One spec's outcome, preferring the last attempt after retries."""
    attempts = test.get("results") or []
    last = attempts[-1] if attempts else {}
    status = (last.get("status") or "").lower()

    if status in ("skipped", "interrupted"):
        return TestResult(name=name, passed=True, error="skipped")

    # `spec.ok` already accounts for expected failures and retries, so trust it
    # over the raw status of the final attempt.
    passed = bool(spec.get("ok")) if "ok" in spec else status == "passed"
    return TestResult(
        name=name,
        passed=passed,
        error=None if passed else _error_text(last) or status or "failed",
        duration_ms=float(last["duration"]) if isinstance(last.get("duration"), (int, float)) else None,
    )


def _error_text(attempt: dict) -> str:
    parts: list[str] = []
    for error in attempt.get("errors") or []:
        message = error.get("message")
        if message:
            parts.append(str(message).strip())
    if not parts and attempt.get("error"):
        parts.append(str(attempt["error"].get("message", "")).strip())
    return "\n\n".join(p for p in parts if p)


class PlaywrightRunner:
    """Drives `playwright test` and parses its JSON report."""

    def list_tests(self, workspace_path: str) -> list[str]:
        raw = self._invoke(workspace_path, ["--list"], timeout=120)
        results = parse_playwright_json(raw.report)
        if results:
            return [r.name for r in results]
        # `--list` on older versions ignores the JSON reporter and prints a
        # plain listing; fall back to that rather than returning nothing.
        return [
            line.strip()
            for line in raw.console.splitlines()
            if line.strip().startswith("[") or " › " in line
        ]

    def run_all(self, workspace_path: str) -> TestRunResult:
        return self._run(workspace_path, [])

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        check_filter_arg(name_substr)
        result = self._run(workspace_path, ["--grep", name_substr])
        if not result.results:
            msg = f"No Playwright tests found matching '{name_substr}'"
            return TestRunResult(
                passed=0, failed=1,
                results=[TestResult(name=name_substr, passed=False, error=msg)],
                raw_output=result.raw_output or msg,
            )
        return result

    # -- internals -----------------------------------------------------------

    def _run(self, workspace_path: str, extra: list[str]) -> TestRunResult:
        raw = self._invoke(workspace_path, extra, timeout=_timeout_seconds())
        results = parse_playwright_json(raw.report)
        return TestRunResult(
            passed=sum(1 for r in results if r.passed),
            failed=sum(1 for r in results if not r.passed),
            results=results,
            raw_output=raw.console,
        )

    def _invoke(self, workspace_path: str, extra: list[str], *, timeout: int) -> "_Invocation":
        report_path = _report_path(workspace_path)
        report_path.unlink(missing_ok=True)
        cmd = node_tool(workspace_path, "playwright") + ["test", "--reporter=json", *extra]

        try:
            proc = run_tool(
                cmd,
                workspace_path=workspace_path,
                timeout=timeout,
                env={"PLAYWRIGHT_JSON_OUTPUT_NAME": str(report_path)},
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolchainError(
                f"`playwright test` timed out after {timeout}s. "
                "Raise WATERFREE_PLAYWRIGHT_TIMEOUT if the suite is genuinely slow."
            ) from exc

        console = f"$ {' '.join(cmd)}\n\n{proc.stdout or ''}{proc.stderr or ''}"
        report = ""
        if report_path.exists():
            report = report_path.read_text(encoding="utf-8", errors="replace")
        elif (proc.stdout or "").lstrip().startswith("{"):
            report = proc.stdout
        return _Invocation(report=report, console=console)


class _Invocation:
    """The two streams a Playwright run produces: the report and the console."""

    __slots__ = ("report", "console")

    def __init__(self, *, report: str, console: str) -> None:
        self.report = report
        self.console = console


def _report_path(workspace_path: str) -> Path:
    path = Path(workspace_path) / ".waterfree" / "testing" / "playwright"
    path.mkdir(parents=True, exist_ok=True)
    return path / "report.json"


def _timeout_seconds() -> int:
    raw = os.environ.get("WATERFREE_PLAYWRIGHT_TIMEOUT", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_TIMEOUT_SECONDS
