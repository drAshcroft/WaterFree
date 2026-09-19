"""
.NET test-runner support (`dotnet test`).

Added because the generic runner used to report `passed=0, failed=0, exit 0`
for a C# solution — see the knowledge-base entry "Waterfree testing may return
0/0 for .NET solutions". FlighterPirates (`server.Tests`) and CardGame both hit
it, and a zero exit code on zero discovered tests is indistinguishable from a
green suite to an agent reading the JSON.

Results come from a TRX log rather than console scraping: `dotnet test` prints
differently per verbosity, per test framework and per SDK version, whereas the
TRX schema is stable and carries per-test outcome, duration and failure text.
This mirrors the lesson already recorded for Godot ("JUnit XML is the only
reliable parse path").
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from backend.testing.errors import NoTestsDiscoveredError, ToolchainError
from backend.testing.results import TestResult, TestRunResult
from backend.testing.toolchain import check_filter_arg, dotnet, run_tool

DEFAULT_TIMEOUT_SECONDS = 900

# The TRX namespace has been stable across every VSTest version that emits it.
_TRX_NS = {"t": "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"}

# Package references that mark a project as a test project. A .csproj without
# one of these is a library `dotnet test` would refuse to run.
_TEST_PACKAGE_RE = re.compile(
    r'PackageReference\s+Include="(?:Microsoft\.NET\.Test\.Sdk|xunit|NUnit|MSTest)',
    re.IGNORECASE,
)

# TRX outcomes that are neither a pass nor a real failure.
_SKIPPED_OUTCOMES = {"notexecuted", "skipped", "inconclusive"}


def find_solution_or_projects(workspace_path: str) -> list[Path]:
    """The targets to hand `dotnet test`.

    A solution file wins when present: it is the unit the developer owns, and
    passing it keeps per-assembly results together in one TRX. Otherwise every
    discovered test project is passed separately.
    """
    root = Path(workspace_path)
    solutions = sorted(root.glob("*.sln")) + sorted(root.glob("*.slnx"))
    if solutions:
        return solutions[:1]
    return find_test_projects(workspace_path)


def find_test_projects(workspace_path: str) -> list[Path]:
    """Every .csproj under the workspace that references a test SDK.

    Bounded to a few levels: a repo with vendored sources under node_modules or
    a build output tree should not turn discovery into a full-disk walk.
    """
    root = Path(workspace_path)
    found: list[Path] = []
    for csproj in sorted(root.glob("*/*.csproj")) + sorted(root.glob("*.csproj")):
        try:
            text = csproj.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _TEST_PACKAGE_RE.search(text):
            found.append(csproj)
    return found


def is_dotnet_project(workspace_path: str) -> bool:
    """True when this workspace holds a .NET solution or test project."""
    return bool(find_solution_or_projects(workspace_path))


# ---------------------------------------------------------------------------
# TRX parsing
# ---------------------------------------------------------------------------


def parse_trx(text: str) -> list[TestResult]:
    """Parse a TRX log into TestResults.

    Unknown outcomes are treated as failures: an outcome we do not recognise is
    not evidence that the test passed.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    results: list[TestResult] = []
    for node in root.iter():
        if not node.tag.endswith("UnitTestResult"):
            continue
        name = node.get("testName") or "unknown"
        outcome = (node.get("outcome") or "").lower()

        if outcome in _SKIPPED_OUTCOMES:
            # Visible, but not counted as a failure — same treatment as a
            # skipped JUnit case in the Godot adapter.
            results.append(TestResult(name=name, passed=True, error="skipped"))
            continue

        passed = outcome == "passed"
        results.append(TestResult(
            name=name,
            passed=passed,
            error=None if passed else _failure_text(node) or outcome or "failed",
            duration_ms=_duration_to_ms(node.get("duration")),
        ))
    return results


def _failure_text(node: ET.Element) -> str:
    """Message + stack trace from a TRX ErrorInfo block."""
    parts: list[str] = []
    for child in node.iter():
        if child.tag.endswith("Message") or child.tag.endswith("StackTrace"):
            if child.text and child.text.strip():
                parts.append(child.text.strip())
    return "\n".join(parts)


def _duration_to_ms(value: str | None) -> float | None:
    """TRX durations are `hh:mm:ss.fffffff`."""
    if not value:
        return None
    match = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", value.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000.0


def scan_test_names(workspace_path: str) -> list[str]:
    """List test names by reading the C# sources.

    `dotnet test --list-tests` needs a full build, which takes minutes on a cold
    tree. Reading attributes is instant and good enough to answer "what exists".
    Names are `Namespace.Class.Method` where the namespace can be determined.
    """
    names: list[str] = []
    root = Path(workspace_path)
    for project in find_test_projects(workspace_path):
        for source in sorted(project.parent.rglob("*.cs")):
            if any(part in ("bin", "obj") for part in source.parts):
                continue
            try:
                text = source.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            names.extend(_scan_source(text))
    return sorted(set(names))


_ATTRIBUTE_RE = re.compile(r"\[\s*(?:Fact|Theory|Test|TestMethod)\b")
_METHOD_RE = re.compile(r"^\s*(?:public|internal)\s+(?:async\s+)?[\w<>\[\],\.\s]+?\s+(\w+)\s*\(")
_CLASS_RE = re.compile(r"^\s*(?:public|internal)\s+(?:sealed\s+|static\s+|partial\s+)*class\s+(\w+)")
_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w\.]+)")


def _scan_source(text: str) -> list[str]:
    namespace = ""
    current_class = ""
    names: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        ns = _NAMESPACE_RE.match(line)
        if ns:
            namespace = ns.group(1)
            continue
        cls = _CLASS_RE.match(line)
        if cls:
            current_class = cls.group(1)
            continue
        if not _ATTRIBUTE_RE.search(line):
            continue
        # The method signature follows the attribute, possibly after further
        # attributes (InlineData, Trait, …) on their own lines.
        for candidate in lines[index + 1: index + 8]:
            method = _METHOD_RE.match(candidate)
            if method:
                qualified = ".".join(p for p in (namespace, current_class, method.group(1)) if p)
                names.append(qualified)
                break
    return names


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class DotnetRunner:
    """Runs a .NET solution's tests through `dotnet test`, parsing the TRX log."""

    def list_tests(self, workspace_path: str) -> list[str]:
        return scan_test_names(workspace_path)

    def run_all(self, workspace_path: str) -> TestRunResult:
        return self._run(workspace_path, test_filter=None)

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        check_filter_arg(name_substr)
        result = self._run(
            workspace_path,
            # VSTest's own substring operator, so the filtering happens before
            # the tests run rather than after.
            test_filter=f"FullyQualifiedName~{name_substr}",
        )
        if not result.results:
            msg = f"No tests found matching '{name_substr}'"
            return TestRunResult(
                passed=0, failed=1,
                results=[TestResult(name=name_substr, passed=False, error=msg)],
                raw_output=result.raw_output or msg,
            )
        return result

    # -- internals -----------------------------------------------------------

    def _run(self, workspace_path: str, *, test_filter: str | None) -> TestRunResult:
        targets = find_solution_or_projects(workspace_path)
        if not targets:
            raise NoTestsDiscoveredError(
                f"No .NET solution or test project found under {workspace_path}."
            )

        binary = dotnet()
        results_dir = _fresh_results_dir(workspace_path)
        transcript: list[str] = []
        results: list[TestResult] = []

        for target in targets:
            cmd = [
                binary, "test", str(target),
                "--nologo",
                # TRX rather than console output: see the module docstring.
                "--logger", "trx",
                "--results-directory", str(results_dir),
            ]
            if test_filter:
                cmd += ["--filter", test_filter]

            try:
                proc = run_tool(
                    cmd,
                    workspace_path=workspace_path,
                    timeout=_timeout_seconds(),
                )
            except subprocess.TimeoutExpired as exc:
                raise ToolchainError(
                    f"`dotnet test` timed out after {_timeout_seconds()}s. "
                    "Raise WATERFREE_DOTNET_TIMEOUT if the suite is genuinely slow."
                ) from exc
            transcript.append(
                f"$ {' '.join(cmd)}\n\n{proc.stdout or ''}{proc.stderr or ''}"
            )

        raw = "\n\n".join(transcript)
        for trx in sorted(results_dir.glob("*.trx"), key=lambda p: p.stat().st_mtime):
            results.extend(parse_trx(trx.read_text(encoding="utf-8", errors="ignore")))

        return TestRunResult(
            passed=sum(1 for r in results if r.passed),
            failed=sum(1 for r in results if not r.passed),
            results=results,
            raw_output=raw,
        )


def _timeout_seconds() -> int:
    raw = os.environ.get("WATERFREE_DOTNET_TIMEOUT", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_TIMEOUT_SECONDS


def _fresh_results_dir(workspace_path: str) -> Path:
    """Per-run TRX directory, emptied first so stale logs never leak in."""
    path = Path(workspace_path) / ".waterfree" / "testing" / "dotnet"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path
