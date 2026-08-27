"""`waterfree testing ...` — workspace test runner CLI."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace, _SubParsersAction

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
from backend.testing.godot import GodotError, GodotRunner
from backend.testing.runners import RUNNERS, detect_runner, read_log, write_log
from backend.testing.summary import summarize_log, summarize_run


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

    p.set_defaults(_runner=run)


def _build_runner(args: Namespace, workspace: str):
    """Honour --runner when given, otherwise auto-detect."""
    godot_path = getattr(args, "godot_path", None)
    forced = getattr(args, "runner", None)
    if forced == "godot":
        return GodotRunner(godot_path=godot_path)
    if forced:
        return RUNNERS[forced]()
    runner = detect_runner(workspace)
    if isinstance(runner, GodotRunner) and godot_path:
        return GodotRunner(godot_path=godot_path)
    return runner


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

    try:
        runner = _build_runner(args, workspace)
        return _dispatch(runner, workspace, args, action)
    except GodotError as exc:
        # Missing engine / project / framework is a setup problem, not a test
        # failure — keep it distinguishable from "your tests are red".
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)


def _dispatch(runner, workspace: str, args: Namespace, action: str) -> int:
    if action == "run":
        result = runner.run_all(workspace)
        write_log(workspace, result.raw_output)
        emit_json(_result_payload(result, workspace, args))
        return EXIT_OK if result.failed == 0 else 1

    if action == "run-one":
        result = runner.run_one(workspace, args.name_substr)
        write_log(workspace, result.raw_output)
        emit_json(_result_payload(result, workspace, args))
        return EXIT_OK if result.failed == 0 and result.passed > 0 else 1

    if action == "list":
        emit_json(runner.list_tests(workspace))
        return EXIT_OK

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)


def _result_payload(result, workspace: str = "", args: Namespace | None = None) -> dict:
    payload = {
        "passed": result.passed,
        "failed": result.failed,
        "total": result.passed + result.failed,
        "results": [
            {
                "name": r.name,
                "passed": r.passed,
                "error": r.error,
                "duration_ms": r.duration_ms,
            }
            for r in result.results
        ],
    }
    if args is not None and getattr(args, "summary", False):
        # Advisory only: a dead gateway must not turn a green suite red, so the
        # failure is reported in-band and the exit code is left to the tests.
        try:
            payload["summary"] = summarize_run(result, workspace_path=workspace)
        except (ChatUnavailable, ValueError) as exc:
            payload["summaryError"] = str(exc)
    return payload
