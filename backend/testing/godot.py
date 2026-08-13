"""
Godot test-runner support for the `waterfree testing` CLI.

Two Godot test frameworks are supported, auto-detected from the addons a
project has installed:

* **gdUnit4** — `addons/gdUnit4/`. Driven through `GdUnitCmdTool.gd`, which
  writes a JUnit XML report we parse for per-test results.
* **GUT** (Godot Unit Test) — `addons/gut/`. Driven through `gut_cmdln.gd`.
  Recent GUT versions can emit JUnit XML via `-gjunit_xml_file`; when that file
  does not appear we fall back to parsing GUT's console summary.

Both paths converge on the same `TestRunResult` shape as the unittest / pytest
/ jest / vitest runners, so callers render Godot results uniformly.

Locating the engine is deliberately explicit. Godot ships many differently
named builds (`godot.windows.editor.double.x86_64.exe`, `Godot_v4.3-stable`,
…), so we never guess by globbing — the binary comes from a setting, an
environment variable, or PATH, in that order.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from backend.testing.results import TestResult, TestRunResult

# Environment variables consulted for the Godot executable, highest first.
GODOT_ENV_VARS = ("WATERFREE_GODOT", "GODOT_BIN", "GODOT")

# Workspace-local config file the VS Code extension mirrors `waterfree.godotPath`
# into, so the standalone CLI can see the setting.
CONFIG_RELPATH = (".waterfree", "config.json")

# Names worth trying on PATH. Ordered plain-first.
PATH_CANDIDATES = ("godot", "godot4", "Godot")

DEFAULT_TIMEOUT_SECONDS = 600

# Conventional test roots, checked in order. Both frameworks default to res://test.
TEST_DIR_CANDIDATES = ("test", "tests")


class GodotError(RuntimeError):
    """Raised when the engine, project, or test framework cannot be resolved."""


# ---------------------------------------------------------------------------
# Engine discovery
# ---------------------------------------------------------------------------


def _config_godot_path(workspace_path: str) -> str | None:
    """Read `godotPath` from the workspace's .waterfree/config.json, if present."""
    config_file = Path(workspace_path).joinpath(*CONFIG_RELPATH)
    if not config_file.exists():
        return None
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    value = data.get("godotPath") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_godot_binary(workspace_path: str, override: str | None = None) -> str:
    """Locate the Godot executable.

    Order: explicit override (CLI flag) → `waterfree.godotPath` mirrored into
    .waterfree/config.json → GODOT env vars → PATH.

    An explicitly configured path that does not resolve is treated as a config
    error rather than a reason to silently fall through to PATH — otherwise a
    typo'd setting quietly runs a different engine build than intended, which is
    exactly the trap when single- and double-precision builds sit side by side.
    """
    configured: list[tuple[str, str]] = []
    if override and override.strip():
        configured.append(("--godot-path", override.strip()))

    from_config = _config_godot_path(workspace_path)
    if from_config:
        configured.append(("waterfree.godotPath", from_config))

    for var in GODOT_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            configured.append((f"${var}", value))

    for _source, value in configured:
        resolved = _usable_binary(value)
        if resolved:
            return resolved

    if configured:
        tried = "; ".join(f"{source}={value}" for source, value in configured)
        raise GodotError(
            f"No runnable Godot executable at any configured location. Tried: {tried}"
        )

    for name in PATH_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found

    raise GodotError(
        "Could not find the Godot executable. Set the 'waterfree.godotPath' "
        "setting, or one of the "
        f"{', '.join(GODOT_ENV_VARS)} environment variables, or put "
        f"one of {', '.join(PATH_CANDIDATES)} on PATH."
    )


def _usable_binary(value: str) -> str | None:
    """Resolve a configured value to a runnable executable, or None.

    A value containing a path separator is treated strictly as a path: if that
    file does not exist we do NOT fall back to a PATH lookup, because silently
    running some other `godot` after a typo'd absolute path is worse than
    failing loudly. A bare name (`godot4`) is looked up on PATH as intended.
    """
    path = Path(value).expanduser()
    if path.is_file():
        return str(path)
    if os.sep in value or (os.altsep and os.altsep in value):
        return None
    return shutil.which(value)


def godot_version(binary: str) -> tuple[int, int] | None:
    """Return (major, minor) from `godot --version`, or None if unparseable.

    Godot prints e.g. `4.3.stable.custom_build` — the trailing build metadata
    varies enough (voxel-tools and other forks append their own) that we only
    trust the leading numeric components.
    """
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)", (proc.stdout or "") + (proc.stderr or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def headless_flag(binary: str) -> str:
    """Godot 4 uses --headless; Godot 3 used --no-window."""
    version = godot_version(binary)
    if version and version[0] <= 3:
        return "--no-window"
    return "--headless"


# ---------------------------------------------------------------------------
# Project & framework discovery
# ---------------------------------------------------------------------------


def find_godot_project(workspace_path: str) -> Path:
    """Find the directory holding project.godot.

    Checked at the workspace root first, then one level down — a common layout
    keeps the engine build and the game project as siblings under one repo.
    """
    root = Path(workspace_path)
    if (root / "project.godot").is_file():
        return root
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if (child / "project.godot").is_file():
            return child
    raise GodotError(f"No project.godot found under {workspace_path}")


def detect_framework(project_root: Path) -> str | None:
    """Return 'gdunit4', 'gut', or None based on installed addons."""
    if (project_root / "addons" / "gdUnit4").is_dir():
        return "gdunit4"
    if (project_root / "addons" / "gut").is_dir():
        return "gut"
    return None


def is_godot_project(workspace_path: str) -> bool:
    """True when this workspace holds a Godot project with a test framework."""
    try:
        project_root = find_godot_project(workspace_path)
    except (GodotError, OSError):
        return False
    return detect_framework(project_root) is not None


def test_directories(project_root: Path) -> list[Path]:
    dirs = [project_root / name for name in TEST_DIR_CANDIDATES]
    return [d for d in dirs if d.is_dir()]


def _res_path(project_root: Path, path: Path) -> str:
    return "res://" + path.relative_to(project_root).as_posix()


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

_TEST_FUNC_RE = re.compile(r"^\s*func\s+(test_\w+)\s*\(", re.MULTILINE)


def scan_test_names(project_root: Path) -> list[str]:
    """List `res://path/to/suite.gd::test_name` ids by reading the .gd sources.

    Reading the files is much faster than booting the engine just to enumerate,
    and it works identically for GUT (`test_*.gd`) and gdUnit4 (`*_test.gd`).
    """
    names: list[str] = []
    for directory in test_directories(project_root):
        for gd_file in sorted(directory.rglob("*.gd")):
            try:
                source = gd_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            res = _res_path(project_root, gd_file)
            names.extend(f"{res}::{m}" for m in _TEST_FUNC_RE.findall(source))
    return names


def parse_junit_xml(text: str) -> list[TestResult]:
    """Parse a JUnit XML report into TestResults.

    Both frameworks emit this format, so it is the preferred path for each.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    suites = root.iter("testsuite")
    results: list[TestResult] = []
    for suite in suites:
        suite_name = suite.get("name") or ""
        for case in suite.findall("testcase"):
            case_name = case.get("name") or "unknown"
            classname = case.get("classname") or suite_name
            name = f"{classname}::{case_name}" if classname else case_name

            failures = case.findall("failure") + case.findall("error")
            skipped = case.find("skipped") is not None
            error = None
            if failures:
                error = "\n".join(
                    "\n".join(part for part in (node.get("message"), (node.text or "").strip()) if part)
                    for node in failures
                ).strip() or "failed"

            results.append(TestResult(
                name=name,
                passed=not failures,
                error=error,
                duration_ms=_seconds_to_ms(case.get("time")),
            ))
            if skipped and not failures:
                # A skipped case is not a failure; keep it visible but passing.
                results[-1].error = "skipped"
    return results


def _seconds_to_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value) * 1000.0
    except ValueError:
        return None


_GUT_TOTALS_RE = re.compile(
    r"Totals.*?Passing tests\s+(\d+).*?Failing tests\s+(\d+)",
    re.IGNORECASE | re.DOTALL,
)


def parse_gut_console(raw: str) -> TestRunResult:
    """Fallback parser for GUT's console summary.

    Only used when GUT did not produce the JUnit XML report — older GUT builds
    lack `-gjunit_xml_file`. Yields counts without per-test detail; callers
    still get the full text via `raw_output`.
    """
    match = _GUT_TOTALS_RE.search(raw)
    if not match:
        return TestRunResult(passed=0, failed=0, results=[], raw_output=raw)
    return TestRunResult(
        passed=int(match.group(1)),
        failed=int(match.group(2)),
        results=[],
        raw_output=raw,
    )


# ---------------------------------------------------------------------------
# Framework strategies
# ---------------------------------------------------------------------------


class GdUnit4Strategy:
    name = "gdunit4"

    def command(self, project_root: Path, report_dir: Path, suites: list[str]) -> list[str]:
        args = [
            "-s", "res://addons/gdUnit4/bin/GdUnitCmdTool.gd",
            # Headless is refused by default because UI tests cannot receive
            # input without a window; we opt in explicitly.
            "--ignoreHeadlessMode",
            # Without --continue gdUnit4 halts on the first failure, which would
            # report a partial suite as if it were the whole one.
            "--continue",
            # Short forms are '-rd' / '-rc' (single dash); '--rd' is not valid.
            "--report-directory", str(report_dir),
            "--report-count", "1",
        ]
        targets = suites or [
            _res_path(project_root, d) for d in test_directories(project_root)
        ]
        if not targets:
            raise GodotError(
                f"No test directory found in {project_root} "
                f"(looked for {', '.join(TEST_DIR_CANDIDATES)})"
            )
        for target in targets:
            args += ["-a", target]
        return args

    def collect(self, report_dir: Path, raw: str) -> TestRunResult:
        # gdUnit4 nests each run under report_dir/report_<n>/results.xml.
        reports = sorted(report_dir.rglob("results.xml"), key=lambda p: p.stat().st_mtime)
        if not reports:
            return TestRunResult(passed=0, failed=0, results=[], raw_output=raw)
        results = parse_junit_xml(reports[-1].read_text(encoding="utf-8", errors="ignore"))
        return _summarize(results, raw)


class GutStrategy:
    name = "gut"

    def command(self, project_root: Path, report_dir: Path, suites: list[str]) -> list[str]:
        xml_path = report_dir / "results.xml"
        args = [
            "-s", "res://addons/gut/gut_cmdln.gd",
            "-gexit",
            "-ginclude_subdirs",
            f"-gjunit_xml_file={xml_path}",
        ]
        if suites:
            for suite in suites:
                args.append(f"-gtest={suite}")
        else:
            dirs = test_directories(project_root)
            if not dirs:
                raise GodotError(
                    f"No test directory found in {project_root} "
                    f"(looked for {', '.join(TEST_DIR_CANDIDATES)})"
                )
            for directory in dirs:
                args.append(f"-gdir={_res_path(project_root, directory)}")
        return args

    def collect(self, report_dir: Path, raw: str) -> TestRunResult:
        xml_path = report_dir / "results.xml"
        if xml_path.exists():
            results = parse_junit_xml(xml_path.read_text(encoding="utf-8", errors="ignore"))
            if results:
                return _summarize(results, raw)
        return parse_gut_console(raw)


STRATEGIES = {"gdunit4": GdUnit4Strategy, "gut": GutStrategy}


def _summarize(results: list[TestResult], raw: str) -> TestRunResult:
    return TestRunResult(
        passed=sum(1 for r in results if r.passed),
        failed=sum(1 for r in results if not r.passed),
        results=results,
        raw_output=raw,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class GodotRunner:
    """Runs a Godot project's tests through whichever framework it installs."""

    def __init__(self, godot_path: str | None = None) -> None:
        self._godot_path_override = godot_path

    # -- TestRunner protocol -------------------------------------------------

    def list_tests(self, workspace_path: str) -> list[str]:
        project_root = find_godot_project(workspace_path)
        return scan_test_names(project_root)

    def run_all(self, workspace_path: str) -> TestRunResult:
        return self._run(workspace_path, suites=[], name_filter=None)

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        project_root = find_godot_project(workspace_path)
        pattern = name_substr.lower()
        suites = sorted({
            test_id.split("::", 1)[0]
            for test_id in scan_test_names(project_root)
            if pattern in test_id.lower()
        })
        if not suites:
            msg = f"No tests found matching '{name_substr}'"
            return TestRunResult(
                passed=0, failed=1,
                results=[TestResult(name=name_substr, passed=False, error=msg)],
                raw_output=msg,
            )
        return self._run(workspace_path, suites=suites, name_filter=pattern)

    # -- internals -----------------------------------------------------------

    def _run(
        self,
        workspace_path: str,
        *,
        suites: list[str],
        name_filter: str | None,
    ) -> TestRunResult:
        project_root = find_godot_project(workspace_path)
        framework = detect_framework(project_root)
        if framework is None:
            raise GodotError(
                f"No supported Godot test framework in {project_root}. "
                "Install addons/gdUnit4 or addons/gut."
            )
        binary = resolve_godot_binary(workspace_path, self._godot_path_override)
        strategy = STRATEGIES[framework]()

        report_dir = _fresh_report_dir(workspace_path, framework)
        args = strategy.command(project_root, report_dir, suites)
        cmd = [binary, headless_flag(binary), "--path", str(project_root), *args]

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=_timeout_seconds(),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise GodotError(
                f"Godot test run timed out after {_timeout_seconds()}s. "
                "Raise WATERFREE_GODOT_TIMEOUT if the suite is genuinely slow."
            ) from exc
        except OSError as exc:
            raise GodotError(f"Could not launch Godot ({binary}): {exc}") from exc

        raw = f"$ {' '.join(cmd)}\n\n{proc.stdout}{proc.stderr}"
        result = strategy.collect(report_dir, raw)

        if name_filter and result.results:
            matched = [r for r in result.results if name_filter in r.name.lower()]
            if matched:
                result = _summarize(matched, raw)

        if not result.results and result.passed == 0 and result.failed == 0:
            # Engine ran but produced nothing parseable — surface it as a failure
            # rather than a silent green.
            return TestRunResult(
                passed=0, failed=1,
                results=[TestResult(
                    name=f"godot:{framework}",
                    passed=False,
                    error=(
                        f"Godot exited with code {proc.returncode} but produced no "
                        "parseable test report. See `waterfree testing logs`."
                    ),
                )],
                raw_output=raw,
            )
        return result


def _timeout_seconds() -> int:
    raw = os.environ.get("WATERFREE_GODOT_TIMEOUT", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_TIMEOUT_SECONDS


def _fresh_report_dir(workspace_path: str, framework: str) -> Path:
    """Per-run report directory, emptied first so stale reports never leak in."""
    report_dir = Path(workspace_path) / ".waterfree" / "testing" / "godot" / framework
    if report_dir.exists():
        shutil.rmtree(report_dir, ignore_errors=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir
