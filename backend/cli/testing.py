"""`waterfree testing ...` — workspace test runner CLI."""

from __future__ import annotations

import os
import sys
import time
from argparse import ArgumentParser, ArgumentTypeError, Namespace, _SubParsersAction

from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    add_full_arg,
    add_workspace_arg,
    emit_error,
    emit_json,
    emit_raw,
    resolve_workspace,
)
from backend.llm.chat_client import ChatUnavailable
from backend.testing.await_log import DEFAULT_MAX_LINES, await_log
from backend.testing.bounded import RunLimits, format_seconds, parse_duration
from backend.testing.errors import (
    NoTestsDiscoveredError,
    NoTestsMatchedError,
    RunIncompleteError,
    TestingError,
)
from backend.testing.godot import GodotRunner
from backend.testing.godot_script import GodotScriptRunner
from backend.testing.results import TestRunResult
from backend.testing.runners import (
    RUNNERS,
    detect_runner,
    other_runners_for,
    read_log,
    runner_name,
    write_log,
)
from backend.testing.summary import summarize_log, summarize_run

# Flags that only mean something to the godot-script runner.
_GODOT_SCRIPT_FLAGS = ("script", "lane", "script_arg", "junit_xml")

DEFAULT_AWAIT_TIMEOUT = "30m"


def _duration(text: str) -> float:
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise ArgumentTypeError(str(exc)) from exc


def _add_runner_args(parser: ArgumentParser) -> None:
    """Flags shared by every action that actually drives a runner."""
    parser.add_argument(
        "--runner",
        choices=sorted(RUNNERS),
        default=None,
        help="Force a specific framework instead of auto-detecting.",
    )
    parser.add_argument(
        "--godot-path",
        default=None,
        help=(
            "Path to the Godot executable. Overrides the waterfree.godotPath "
            "setting and the GODOT environment variables."
        ),
    )
    parser.add_argument(
        "--budget",
        type=_duration,
        default=None,
        metavar="DURATION",
        help=(
            "Wall-time limit for the whole run (e.g. 90s, 10m, 1h30m). When it "
            "is exceeded the test process tree is killed and the result is "
            "incomplete (exit 4). Replaces the runner's default timeout."
        ),
    )
    parser.add_argument(
        "--stall",
        type=_duration,
        default=None,
        metavar="DURATION",
        help=(
            "Kill the run after this long with no stdout/stderr output (e.g. "
            "2m). Catches a headless engine hung on a parse error. Incomplete, "
            "exit 4."
        ),
    )
    group = parser.add_argument_group("godot-script runner")
    group.add_argument(
        "--script",
        default=None,
        metavar="RES_PATH",
        help=(
            "SceneTree entry script for --runner godot-script, e.g. "
            "res://tests/run_all.gd. Implies --runner godot-script."
        ),
    )
    group.add_argument(
        "--lane",
        action="append",
        default=[],
        metavar="NAME",
        help="Lane passed to the script after `--` (repeatable).",
    )
    group.add_argument(
        "--arg",
        dest="script_arg",
        action="append",
        default=[],
        metavar="VALUE",
        help="Extra argument passed to the script after `--` (repeatable).",
    )
    group.add_argument(
        "--junit-xml",
        default=None,
        metavar="PATH",
        help=(
            "Where the script writes a JUnit report, if it uses a fixed path "
            "(res:// or filesystem). Scripts can also write to "
            "$WATERFREE_JUNIT_XML, which is always set."
        ),
    )


def _add_summary_arg(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--summary",
        action="store_true",
        help=(
            "Add an LLM root-cause summary of the failures to the JSON output. "
            "Uses the `testing` stage from .waterfree/providers.json, which "
            "defaults to a free OpenRouter model and falls back to local Ollama."
        ),
    )


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("testing", help="Auto-detected test runner")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_run = actions.add_parser("run", help="Run the full test suite")
    add_workspace_arg(p_run)
    _add_runner_args(p_run)
    _add_summary_arg(p_run)
    add_full_arg(p_run)

    p_run_one = actions.add_parser("run-one", help="Run tests matching a substring")
    add_workspace_arg(p_run_one)
    p_run_one.add_argument("name_substr")
    _add_runner_args(p_run_one)
    _add_summary_arg(p_run_one)
    add_full_arg(p_run_one)

    p_list = actions.add_parser("list", help="Discover all test names")
    add_workspace_arg(p_list)
    _add_runner_args(p_list)
    add_full_arg(p_list)

    p_logs = actions.add_parser("logs", help="Print raw output from the last run")
    add_workspace_arg(p_logs)

    p_summary = actions.add_parser(
        "summarize",
        help="LLM root-cause summary of the last run's stored output",
    )
    add_workspace_arg(p_summary)

    p_await = actions.add_parser(
        "await",
        help=(
            "Block until a log gains a line matching --until (or --pid exits, "
            "or --timeout), then print only the summary lines"
        ),
    )
    add_workspace_arg(p_await)
    p_await.add_argument(
        "--log", required=True,
        help="Log file to watch. Relative paths resolve against the current directory.",
    )
    p_await.add_argument(
        "--until", required=True, metavar="REGEX",
        help="Return as soon as a line appended after the call started matches this.",
    )
    p_await.add_argument(
        "--timeout", type=_duration, default=parse_duration(DEFAULT_AWAIT_TIMEOUT),
        metavar="DURATION",
        help=(
            f"Give up after this long (default {DEFAULT_AWAIT_TIMEOUT}). Keep it "
            "below your own tool-call timeout so you get the JSON back."
        ),
    )
    p_await.add_argument(
        "--pid", type=int, default=None,
        help="Also return (reason pid_exited) when this process exits without a match.",
    )
    p_await.add_argument(
        "--summary-pattern", default=None, metavar="REGEX",
        help="Which lines to report (default: PASS/FAIL/SUMMARY/Parse Error/SCRIPT ERROR/N passed).",
    )
    p_await.add_argument(
        "--max-lines", type=int, default=DEFAULT_MAX_LINES,
        help=f"Cap on summaryLines (default {DEFAULT_MAX_LINES}); first and last lines are kept.",
    )
    mode = p_await.add_mutually_exclusive_group()
    mode.add_argument(
        "--fresh", action="store_true",
        help=(
            "Ignore the existing file entirely, even lines appended to it: only "
            "a log created or rewritten after the call started counts. Use when "
            "an old process may still be writing to the same path."
        ),
    )
    mode.add_argument(
        "--include-existing", action="store_true",
        help="Also scan content already in the log. Risks matching a stale run.",
    )

    p.set_defaults(_runner=run)


def _build_runner(args: Namespace, workspace: str):
    """Honour --runner when given, otherwise auto-detect.

    Returns (runner, auto_detected). `--script` implies godot-script, and the
    godot-script-only flags are refused for any other runner rather than
    silently ignored.
    """
    godot_path = getattr(args, "godot_path", None)
    forced = getattr(args, "runner", None)
    if forced is None and getattr(args, "script", None):
        forced = "godot-script"

    used = [f for f in _GODOT_SCRIPT_FLAGS if getattr(args, f, None)]
    if used and forced != "godot-script":
        flags = ", ".join("--" + ("arg" if f == "script_arg" else f.replace("_", "-")) for f in used)
        raise ValueError(f"{flags} only apply to --runner godot-script.")

    if forced == "godot-script":
        runner = GodotScriptRunner(
            godot_path=godot_path,
            script=getattr(args, "script", None),
            lanes=list(getattr(args, "lane", []) or []),
            extra_args=list(getattr(args, "script_arg", []) or []),
            junit_xml=getattr(args, "junit_xml", None),
        )
        return runner, False
    if forced == "godot":
        return GodotRunner(godot_path=godot_path), False
    if forced:
        return RUNNERS[forced](), False
    runner = detect_runner(workspace)
    if isinstance(runner, GodotRunner) and godot_path:
        runner = GodotRunner(godot_path=godot_path)
    return runner, True


def run(args: Namespace) -> int:
    workspace = resolve_workspace(args)
    action = args.action

    if action == "logs":
        emit_raw(read_log(workspace))
        return EXIT_OK

    if action == "summarize":
        try:
            summary = summarize_log(read_log(workspace), workspace_path=workspace)
        except ChatUnavailable as exc:
            return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
        except ValueError as exc:
            return emit_error(str(exc), exit_code=EXIT_USAGE)
        emit_json({"summary": summary})
        return EXIT_OK

    if action == "await":
        return _await(args)

    runner = None
    auto = False
    started = time.monotonic()
    try:
        runner, auto = _build_runner(args, workspace)
        budget, stall = getattr(args, "budget", None), getattr(args, "stall", None)
        if budget or stall:
            runner.limits = RunLimits(budget=budget, stall=stall)
        return _dispatch(runner, workspace, args, action)
    except NoTestsMatchedError as exc:
        return _emit_no_match(exc, workspace, auto=auto)
    except RunIncompleteError as exc:
        return _emit_incomplete(exc, runner, workspace, time.monotonic() - started)
    except TestingError as exc:
        # A missing engine, absent toolchain, undetectable framework or empty
        # discovery is a setup problem, not a test failure — keep it
        # distinguishable from "your tests are red", which is exit 1.
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)


def _dispatch(runner, workspace: str, args: Namespace, action: str) -> int:
    started = time.monotonic()
    if action == "run":
        result = runner.run_all(workspace)
        write_log(workspace, result.raw_output)
        if result.incomplete:
            return _emit_incomplete_result(result, runner, workspace, args, started)
        _reject_empty_run(runner, result, workspace)
        emit_json(_result_payload(result, workspace, args, runner=runner, started=started))
        return EXIT_OK if result.failed == 0 else 1

    if action == "run-one":
        result = runner.run_one(workspace, args.name_substr)
        write_log(workspace, result.raw_output)
        if result.incomplete:
            return _emit_incomplete_result(result, runner, workspace, args, started)
        if result.passed == 0 and result.failed == 0:
            # Backstop for any runner that comes back empty-handed rather than
            # raising: nothing executed is a selection problem, not a red test.
            raise NoTestsMatchedError(
                runner=runner_name(runner),
                filter=args.name_substr,
                skipped=result.skipped,
                raw_output=result.raw_output,
            )
        emit_json(_result_payload(result, workspace, args, runner=runner, started=started))
        return EXIT_OK if result.failed == 0 else 1

    if action == "list":
        names = runner.list_tests(workspace)
        if not names:
            raise NoTestsDiscoveredError(
                f"{runner_name(runner)} found no tests in {workspace}. "
                "An empty list is not evidence that a suite is green — check "
                "the framework is installed and the tests are where it looks."
            )
        emit_json(names)
        return EXIT_OK

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)


def _reject_empty_run(runner, result, workspace: str) -> None:
    """Refuse to report a zero-test run as a success.

    `passed=0, failed=0, exit 0` is indistinguishable from a green suite to
    every caller, and it is what the old fallback produced for Paradoxia and
    for .NET solutions. Zero discovered tests is a setup failure.
    """
    if result.passed or result.failed:
        return
    raise NoTestsDiscoveredError(
        f"{runner_name(runner)} ran but discovered no tests in {workspace}. "
        "Treat this as a setup failure, not a passing suite. "
        "Run `waterfree testing logs` for the runner's own output."
    )


def _result_payload(
    result,
    workspace: str = "",
    args: Namespace | None = None,
    *,
    runner=None,
    started: float | None = None,
) -> dict:
    payload: dict = {}
    if runner is not None:
        payload["runner"] = runner_name(runner)
    payload.update({
        "passed": result.passed,
        "failed": result.failed,
        "skipped": result.skipped,
        "total": result.passed + result.failed + result.skipped,
        "results": [
            {
                "name": r.name,
                "passed": r.passed,
                "skipped": r.skipped,
                "error": r.error,
                "duration_ms": r.duration_ms,
            }
            for r in result.results
        ],
    })
    if started is not None:
        payload["elapsedSeconds"] = round(time.monotonic() - started, 2)
    if getattr(result, "incomplete", False):
        payload["incomplete"] = True
        payload["reason"] = result.incomplete_reason
    if getattr(result, "first_error", None):
        payload["firstError"] = result.first_error
        payload["firstErrorLocation"] = result.first_error_location
    if getattr(result, "receipt", None):
        payload["receipt"] = result.receipt
    if args is not None and getattr(args, "summary", False):
        # Advisory only: a dead gateway must not turn a green suite red, so the
        # failure is reported in-band and the exit code is left to the tests.
        try:
            payload["summary"] = summarize_run(result, workspace_path=workspace)
        except (ChatUnavailable, ValueError) as exc:
            payload["summaryError"] = str(exc)
    return payload


def _emit_incomplete_result(result, runner, workspace: str, args: Namespace, started: float) -> int:
    """A run that finished without a verdict (godot-script with no summary)."""
    payload = _result_payload(result, workspace, None, runner=runner, started=started)
    message = (
        f"{runner_name(runner)} run is incomplete ({result.incomplete_reason}): "
        "no verdict was produced, so this is not a pass."
    )
    payload["error"] = message
    emit_json(payload)
    sys.stderr.write(f"error: {message} See `waterfree testing logs`.\n")
    return EXIT_DEP_MISSING


def _emit_incomplete(exc: RunIncompleteError, runner, workspace: str, elapsed: float) -> int:
    """A run killed by --budget / --stall: exit 4 with whatever was gathered."""
    result = exc.partial if isinstance(exc.partial, TestRunResult) else TestRunResult(passed=0, failed=0)
    result.incomplete = True
    result.incomplete_reason = exc.reason
    result.first_error = result.first_error or exc.first_error
    result.first_error_location = result.first_error_location or exc.first_error_location
    write_log(workspace, exc.raw_output or result.raw_output)
    payload = _result_payload(result, workspace, None, runner=runner)
    payload["elapsedSeconds"] = round(max(exc.elapsed, elapsed), 2)
    if exc.limit is not None:
        payload["limitSeconds"] = round(exc.limit, 2)
    payload["error"] = str(exc)
    emit_json(payload)
    sys.stderr.write(f"error: {exc}\n")
    return EXIT_DEP_MISSING


def _emit_no_match(exc: NoTestsMatchedError, workspace: str, *, auto: bool) -> int:
    """run-one matched nothing: a JSON error on stdout, exit 4.

    When the runner was auto-detected, name the other runners this workspace
    has evidence for — a C# test name in a repo that also has Python tests
    should not dead-end on "unittest found nothing".
    """
    if exc.raw_output:
        write_log(workspace, exc.raw_output)
    payload: dict = {
        "error": str(exc),
        "code": "no_tests_matched",
        "runner": exc.runner,
        "filter": exc.filter,
        "available": exc.available,
        "skipped": exc.skipped,
    }
    if auto:
        others = other_runners_for(workspace, exc.runner, exc.filter)
        payload["autoDetected"] = True
        payload["otherRunners"] = others
        hint = _retry_hint(exc.runner, others)
        if hint:
            payload["hint"] = hint
    emit_json(payload)
    sys.stderr.write(
        f"error: no tests matched '{exc.filter}' (runner: {exc.runner}); "
        "details are in the JSON on stdout.\n"
    )
    return EXIT_DEP_MISSING


def _retry_hint(chosen: str, others: list[dict]) -> str | None:
    if not others:
        return None
    with_matches = [o for o in others if o.get("matches")]
    if with_matches:
        best = with_matches[0]
        return (
            f"{chosen} was auto-detected, but {best['matches']} {best['runner']} "
            f"test(s) match this name ({best['evidence']}). Retry with "
            f"--runner {best['runner']}."
        )
    names = ", ".join(f"{o['runner']} ({o['evidence']})" for o in others)
    return (
        f"{chosen} was auto-detected; this workspace also has {names}. If the "
        "test belongs to one of those, retry with --runner <name>."
    )


def _await(args: Namespace) -> int:
    log = os.path.abspath(args.log)
    try:
        outcome = await_log(
            log,
            args.until,
            timeout=args.timeout,
            pid=args.pid,
            summary_pattern=args.summary_pattern,
            max_lines=args.max_lines,
            fresh=args.fresh,
            include_existing=args.include_existing,
        )
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)
    emit_json(outcome.to_dict())
    if outcome.matched:
        return EXIT_OK
    detail = {
        "pid_exited": f"process {args.pid} exited before a line matched",
        "timeout": f"no new line matched within {format_seconds(args.timeout)}",
    }.get(outcome.reason, outcome.reason)
    if not outcome.log_exists:
        detail += f"; {log} never appeared"
    sys.stderr.write(f"error: await stopped without a match: {detail}.\n")
    return EXIT_DEP_MISSING
