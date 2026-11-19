"""
Custom Godot SceneTree script runner (`--runner godot-script`).

Not every Godot project tests through GUT or gdUnit4. BrickWild's QA is
`tests/run_all.gd` (`extends SceneTree`) with named lanes, PaperFjord has
standalone smoke scripts, and agents were calling Godot by hand and writing
their own receipts for them (KB 3b7c22e5, 56572c91, 707fe379). This runner
brings those harnesses under the same bounded, parseable receipts as every
other runner:

    godot --headless --path <project> --script res://tests/run_all.gd -- <lanes/args>

* **Bounded.** Runs through `run_bounded`, so `--budget` / `--stall` kill a
  headless engine that hangs on a parse error instead of costing the caller
  its whole tool timeout. The engine's own timeout (WATERFREE_GODOT_TIMEOUT,
  default 1800s) also ends as *incomplete*, never as a pass.
* **Parsed.** Lane summary lines — `<name>  PASS|FAIL  (n checked, m failures[, k warnings])`,
  BrickWild's format — become one result each. A JUnit report, when the
  script writes one to `$WATERFREE_JUNIT_XML` or `--junit-xml`, is preferred.
* **Never a false green.** No lane summary and no JUnit report = incomplete
  (exit 4). A non-zero native exit with no failing lane is still a failure.
* **Receipted.** Native exit code, the exact command, and a source fingerprint
  (git HEAD + whether tracked files were modified) go in the JSON receipt; full
  stdout and stderr go in `.waterfree/testing/godot-script/receipt.json` and
  the testing log.

`list` asks the script for its lanes with `-- --list-lanes`. The convention is
one `LANE <name>` line per lane; failing that, a `known lanes: a, b` /
`known suites: c, d` line (what BrickWild prints for an unknown argument) is
accepted, so existing harnesses work without changes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.testing.bounded import RunLimits, first_script_error, run_bounded
from backend.testing.errors import NoTestsDiscoveredError, RunIncompleteError
from backend.testing.godot import (
    CONFIG_RELPATH,
    GodotError,
    _config_search_roots,
    _timeout_seconds,
    find_godot_project,
    headless_flag,
    parse_junit_xml,
    resolve_godot_binary,
    scene_tree_scripts,
)
from backend.testing.results import TestResult, TestRunResult

LIST_LANES_ARG = "--list-lanes"
JUNIT_ENV_VAR = "WATERFREE_JUNIT_XML"

# `list` boots the engine just to print names; it should never take long.
LIST_TIMEOUT_SECONDS = 300

# `massing            PASS  (12 checked, 0 failures, 2 warnings)` — BrickWild's
# SuiteResult.summary(). Also accepts the status first: `PASS massing (3 checked, 0 failures)`.
_LANE_RE = re.compile(
    r"^\s*(?:(?P<name>\S.*?)\s+)?(?P<status>PASS|FAIL)\b[\s:-]*(?P<after>.*?)\s*"
    r"\(\s*(?P<checked>\d+)\s+checked\s*,\s*(?P<failures>\d+)\s+failures?"
    r"(?:\s*,\s*(?P<warnings>\d+)\s+warnings?)?\s*\)\s*$"
)
_BLOCK_HEADER_RE = re.compile(r"^---\s+(?P<name>.+?)\s+---\s*$")
_BLOCK_FAIL_RE = re.compile(r"^\s*FAIL\s+(?P<detail>.+)$")
_BLOCK_ELAPSED_RE = re.compile(r"\belapsed\s+(?P<seconds>\d+(?:\.\d+)?)s\b")
_VERDICT_RE = re.compile(r"^\s*(?:ALL PASS|\d+\s+SUITES?(?:\(S\))?\s+FAILED)\b.*$")
_LIST_LANE_RE = re.compile(r"^\s*LANE[:\s]+(?P<name>\S+)\s*$")
_KNOWN_RE = re.compile(r"known (?P<kind>lanes|suites):\s*(?P<names>[^;\n]+)")

_CONFIG_SCRIPT_KEYS = ("godotTestScript", "waterfree.godotTestScript")


class GodotScriptRunner:
    """Runs a SceneTree entry script headless and parses its lane summaries."""

    name = "godot-script"
    limits: RunLimits | None = None

    def __init__(
        self,
        godot_path: str | None = None,
        script: str | None = None,
        lanes: list[str] | None = None,
        extra_args: list[str] | None = None,
        junit_xml: str | None = None,
    ) -> None:
        self._godot_path_override = godot_path
        self._script = script
        self._lanes = list(lanes or [])
        self._extra_args = list(extra_args or [])
        self._junit_xml = junit_xml

    # -- TestRunner protocol -------------------------------------------------

    def list_tests(self, workspace_path: str) -> list[str]:
        invocation = self._invoke(workspace_path, [LIST_LANES_ARG], listing=True)
        names = parse_lane_listing(invocation.stdout + "\n" + invocation.stderr)
        if not names:
            raise NoTestsDiscoveredError(
                f"{invocation.script} printed no lanes for `-- {LIST_LANES_ARG}` "
                f"(exit {invocation.returncode}). The convention is one "
                "`LANE <name>` line per lane, then exit 0."
            )
        return names

    def run_all(self, workspace_path: str) -> TestRunResult:
        return self._run(workspace_path, self._lanes + self._extra_args)

    def run_one(self, workspace_path: str, name_substr: str) -> TestRunResult:
        # The script decides what a name means (a lane, a suite); we pass it
        # through as one more lane argument.
        return self._run(workspace_path, self._lanes + [name_substr] + self._extra_args)

    # -- internals -----------------------------------------------------------

    def _run(self, workspace_path: str, user_args: list[str]) -> TestRunResult:
        invocation = self._invoke(workspace_path, user_args, listing=False)
        stdout, stderr = invocation.stdout, invocation.stderr
        lanes = parse_lane_summaries(stdout)
        junit_results, junit_path = self._read_junit(invocation)
        first_error, location = first_script_error(stderr, stdout)
        verdict = next((line.strip() for line in reversed(stdout.splitlines())
                        if _VERDICT_RE.match(line)), None)

        receipt = invocation.receipt()
        receipt.update({
            "lanes": [lane.to_dict() for lane in lanes],
            "verdict": verdict,
            "junit": str(junit_path) if junit_path else None,
        })
        if first_error:
            receipt["firstError"] = first_error
        _write_receipt_file(workspace_path, receipt, stdout, stderr)
        receipt["receiptFile"] = str(_receipt_path(workspace_path))

        raw = invocation.transcript()
        if junit_results:
            results = junit_results
        elif lanes:
            results = [lane.to_result() for lane in lanes]
        else:
            receipt["outputTail"] = _tail_lines(stderr or stdout)
            return TestRunResult(
                passed=0,
                failed=0,
                raw_output=raw,
                incomplete=True,
                incomplete_reason="no_summary",
                first_error=first_error,
                first_error_location=location,
                receipt=receipt,
            )

        if invocation.returncode != 0 and all(r.passed or r.skipped for r in results):
            # The exit code is the script's own verdict. A crash after the
            # last lane, or failures reported outside any lane (BrickWild's
            # "engine script errors" block), must not come back green.
            stray = [m.group("detail") for m in map(_BLOCK_FAIL_RE.match, stdout.splitlines()) if m]
            detail = "\n".join(stray[:10]) or "\n".join(_tail_lines(stderr or stdout))
            results.append(TestResult(
                name="godot-script exit code",
                passed=False,
                error=(
                    f"{invocation.script} exited {invocation.returncode} although no "
                    f"lane reported FAIL.\n{detail}"
                ).strip(),
            ))

        return TestRunResult(
            passed=sum(1 for r in results if r.passed),
            failed=sum(1 for r in results if not r.passed and not r.skipped),
            results=results,
            raw_output=raw,
            first_error=first_error,
            first_error_location=location,
            receipt=receipt,
        )

    def _invoke(self, workspace_path: str, user_args: list[str], *, listing: bool) -> _Invocation:
        project_root = find_godot_project(workspace_path)
        script = resolve_script(workspace_path, project_root, self._script)
        binary = resolve_godot_binary(workspace_path, self._godot_path_override)
        fingerprint = source_fingerprint(project_root)

        report_dir = _fresh_report_dir(workspace_path)
        junit_env_path = report_dir / "junit.xml"
        cmd = [binary, headless_flag(binary), "--path", str(project_root), "--script", script]
        if user_args:
            cmd += ["--", *user_args]

        invocation = _Invocation(
            cmd=cmd, script=script, project_root=project_root,
            fingerprint=fingerprint, junit_env_path=junit_env_path,
            junit_explicit=_resolve_junit(project_root, self._junit_xml),
        )
        timeout = LIST_TIMEOUT_SECONDS if listing else _timeout_seconds()
        try:
            proc = run_bounded(
                cmd,
                cwd=str(project_root),
                timeout=timeout,
                env={JUNIT_ENV_VAR: str(junit_env_path)},
                limits=self.limits,
            )
        except subprocess.TimeoutExpired as exc:
            # New runner, so no legacy contract to keep: the engine's own
            # timeout is just another way of not getting a verdict.
            stdout = _text(exc.stdout)
            stderr = _text(exc.stderr)
            first_error, location = first_script_error(stderr, stdout)
            message = (
                f"godot-script run hit its {timeout}s timeout and was killed; no "
                "verdict. Pass --budget to allow longer, --stall to fail faster on a hang."
            )
            raise self._incomplete(
                invocation, message, reason="timeout", elapsed=float(timeout),
                limit=float(timeout), stdout=stdout, stderr=stderr,
                first_error=first_error, location=location, workspace_path=workspace_path,
            ) from exc
        except RunIncompleteError as exc:
            receipt = invocation.receipt()
            exc.partial = TestRunResult(passed=0, failed=0, receipt=receipt,
                                        raw_output=exc.raw_output)
            raise
        except OSError as exc:
            raise GodotError(f"Could not launch Godot ({binary}): {exc}") from exc

        invocation.finish(proc)
        return invocation

    def _incomplete(self, invocation, message, *, reason, elapsed, limit, stdout, stderr,
                    first_error, location, workspace_path) -> RunIncompleteError:
        receipt = invocation.receipt()
        receipt["outputTail"] = _tail_lines(stderr or stdout)
        raw = f"$ {' '.join(invocation.cmd)}\n\n{stdout}{stderr}\n[waterfree] {reason}: {message}\n"
        return RunIncompleteError(
            message, reason=reason, elapsed=elapsed, limit=limit,
            first_error=first_error, first_error_location=location, raw_output=raw,
            partial=TestRunResult(passed=0, failed=0, receipt=receipt, raw_output=raw),
        )

    def _read_junit(self, invocation: _Invocation) -> tuple[list[TestResult], Path | None]:
        """A JUnit report written by this run, preferred over console parsing."""
        for candidate in (invocation.junit_explicit, invocation.junit_env_path):
            if candidate is None or not candidate.is_file():
                continue
            # A fixed-path report from an earlier run is not this run's result.
            if candidate.stat().st_mtime + 2 < invocation.started_wall:
                continue
            results = parse_junit_xml(candidate.read_text(encoding="utf-8", errors="ignore"))
            if results:
                return results, candidate
        return [], None


class _Invocation:
    """One engine launch: what was run, on which source, and what came back."""

    def __init__(self, *, cmd, script, project_root, fingerprint, junit_env_path, junit_explicit):
        self.cmd = [str(c) for c in cmd]
        self.script = script
        self.project_root = project_root
        self.fingerprint = fingerprint
        self.junit_env_path = junit_env_path
        self.junit_explicit = junit_explicit
        self.started_wall = time.time()
        self.started_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.started = time.monotonic()
        self.elapsed: float | None = None
        self.returncode: int | None = None
        self.stdout = ""
        self.stderr = ""

    def finish(self, proc: subprocess.CompletedProcess) -> None:
        self.elapsed = time.monotonic() - self.started
        self.returncode = proc.returncode
        self.stdout = proc.stdout or ""
        self.stderr = proc.stderr or ""

    def receipt(self) -> dict:
        return {
            "script": self.script,
            "command": self.cmd,
            "projectRoot": str(self.project_root),
            "exitCode": self.returncode,
            "startedAt": self.started_iso,
            "elapsedSeconds": round(self.elapsed, 2) if self.elapsed is not None else None,
            "fingerprint": self.fingerprint,
            "stdoutBytes": len(self.stdout.encode("utf-8")),
            "stderrBytes": len(self.stderr.encode("utf-8")),
        }

    def transcript(self) -> str:
        return (
            f"$ {' '.join(self.cmd)}\n\n{self.stdout}"
            + (f"\n--- stderr ---\n{self.stderr}" if self.stderr else "")
            + f"\n[waterfree] godot-script exit code {self.returncode}\n"
        )


class LaneSummary:
    __slots__ = ("checked", "details", "elapsed", "failures", "name", "status", "warnings")

    def __init__(self, name, status, checked, failures, warnings):
        self.name = name
        self.status = status
        self.checked = checked
        self.failures = failures
        self.warnings = warnings
        self.details: list[str] = []
        self.elapsed: float | None = None

    @property
    def passed(self) -> bool:
        return self.status == "PASS" and self.failures == 0

    def to_dict(self) -> dict:
        payload = {"name": self.name, "status": self.status, "checked": self.checked,
                   "failures": self.failures}
        if self.warnings is not None:
            payload["warnings"] = self.warnings
        return payload

    def to_result(self) -> TestResult:
        error = None
        if not self.passed:
            error = "\n".join(self.details[:20]) or (
                f"{self.failures} failure(s) in {self.checked} checks"
            )
        return TestResult(
            name=self.name,
            passed=self.passed,
            error=error,
            duration_ms=self.elapsed * 1000.0 if self.elapsed is not None else None,
        )


def parse_lane_summaries(stdout: str) -> list[LaneSummary]:
    """Lane summary lines, with failure detail from the matching `--- name ---` block."""
    blocks: dict[str, tuple[list[str], float | None]] = {}
    current: str | None = None
    for line in stdout.splitlines():
        header = _BLOCK_HEADER_RE.match(line.strip())
        if header:
            current = header.group("name").strip()
            blocks[current] = ([], None)
            continue
        if current is None:
            continue
        if not line.strip():
            current = None
            continue
        details, elapsed = blocks[current]
        fail = _BLOCK_FAIL_RE.match(line)
        if fail:
            details.append(fail.group("detail").strip())
        seconds = _BLOCK_ELAPSED_RE.search(line)
        if seconds and elapsed is None:
            blocks[current] = (details, float(seconds.group("seconds")))

    lanes: list[LaneSummary] = []
    for line in stdout.splitlines():
        match = _LANE_RE.match(line)
        if not match:
            continue
        name = (match.group("name") or match.group("after") or "").strip() or f"lane {len(lanes) + 1}"
        warnings = match.group("warnings")
        lane = LaneSummary(
            name=name,
            status=match.group("status"),
            checked=int(match.group("checked")),
            failures=int(match.group("failures")),
            warnings=int(warnings) if warnings is not None else None,
        )
        if name in blocks:
            lane.details, lane.elapsed = blocks[name]
        lanes.append(lane)
    return lanes


def parse_lane_listing(text: str) -> list[str]:
    """Lane names from `--list-lanes` output; see the module docstring."""
    names = [m.group("name") for m in map(_LIST_LANE_RE.match, text.splitlines()) if m]
    if not names:
        by_kind: dict[str, list[str]] = {"lanes": [], "suites": []}
        for match in _KNOWN_RE.finditer(text):
            by_kind[match.group("kind")] += [
                n.strip() for n in match.group("names").split(",") if n.strip()
            ]
        names = by_kind["lanes"] + by_kind["suites"]
    return list(dict.fromkeys(n for n in names if n != LIST_LANES_ARG))


# ---------------------------------------------------------------------------
# Script, report and receipt paths
# ---------------------------------------------------------------------------


def resolve_script(workspace_path: str, project_root: Path, script: str | None) -> str:
    """The `res://` path to run, validated to exist.

    From `--script`, else `godotTestScript` in .waterfree/config.json. A
    filesystem path inside the project is converted to `res://`; a bare
    relative path is taken relative to the project root.
    """
    value = (script or "").strip() or _configured_script(workspace_path)
    if not value:
        candidates = scene_tree_scripts(workspace_path)
        hint = f" SceneTree scripts found: {', '.join(candidates)}." if candidates else ""
        raise GodotError(
            "--runner godot-script needs --script res://path/to/runner.gd "
            f"(or godotTestScript in .waterfree/config.json).{hint}"
        )

    if value.startswith("res://"):
        relative = value[len("res://"):]
    else:
        path = Path(value).expanduser()
        if not path.is_absolute() and not path.exists():
            path = project_root / path
        try:
            relative = path.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError as exc:
            raise GodotError(
                f"Script {value} is not inside the Godot project at {project_root}."
            ) from exc

    target = project_root.joinpath(*relative.split("/"))
    if not target.is_file():
        raise GodotError(f"Script res://{relative} not found (looked for {target}).")
    return f"res://{relative}"


def _configured_script(workspace_path: str) -> str | None:
    for root in _config_search_roots(workspace_path):
        config_file = Path(root).joinpath(*CONFIG_RELPATH)
        try:
            data = json.loads(config_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        nested = data.get("waterfree")
        values = [data.get(key) for key in _CONFIG_SCRIPT_KEYS]
        if isinstance(nested, dict):
            values.append(nested.get("godotTestScript"))
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _resolve_junit(project_root: Path, junit_xml: str | None) -> Path | None:
    if not junit_xml:
        return None
    if junit_xml.startswith("res://"):
        return project_root.joinpath(*junit_xml[len("res://"):].split("/"))
    return Path(junit_xml).expanduser().resolve()


def _fresh_report_dir(workspace_path: str) -> Path:
    path = Path(workspace_path) / ".waterfree" / "testing" / "godot-script"
    junit = path / "junit.xml"
    junit.unlink(missing_ok=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _receipt_path(workspace_path: str) -> Path:
    return Path(workspace_path) / ".waterfree" / "testing" / "godot-script" / "receipt.json"


def _write_receipt_file(workspace_path: str, receipt: dict, stdout: str, stderr: str) -> None:
    path = _receipt_path(workspace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({**receipt, "stdout": stdout, "stderr": stderr}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Source fingerprint
# ---------------------------------------------------------------------------


def source_fingerprint(path: Path) -> dict:
    """git HEAD and whether tracked files differ from it, for the receipt.

    Untracked files are deliberately not counted: test runs write their own
    outputs into the tree, and a fingerprint that flips to dirty because of
    the previous run's report says nothing about the code under test.
    """
    git = shutil.which("git")
    if not git:
        return {"head": None, "dirty": None, "error": "git not found on PATH"}
    try:
        head = subprocess.run(
            [git, "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
        )
        if head.returncode != 0:
            return {"head": None, "dirty": None, "error": "not a git repository (or no commits)"}
        status = subprocess.run(
            [git, "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"head": None, "dirty": None, "error": f"git failed: {exc}"}
    changed = [line for line in (status.stdout or "").splitlines() if line.strip()]
    return {
        "head": head.stdout.strip(),
        "dirty": bool(changed) if status.returncode == 0 else None,
        "dirtyFiles": len(changed),
    }


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _tail_lines(text: str, count: int = 8, width: int = 300) -> list[str]:
    lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    return [line if len(line) <= width else line[:width] + "…" for line in lines[-count:]]
