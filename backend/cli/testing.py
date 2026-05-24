"""`waterfree testing ...` — workspace test runner CLI."""

from __future__ import annotations

import json
import sys
from argparse import Namespace, _SubParsersAction

from backend.cli._common import (
    EXIT_OK,
    EXIT_USAGE,
    add_workspace_arg,
    emit_error,
    emit_json,
    emit_raw,
    resolve_workspace,
)
from backend.testing.runners import detect_runner, read_log, write_log


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("testing", help="Auto-detected test runner")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_run = actions.add_parser("run", help="Run the full test suite")
    add_workspace_arg(p_run)

    p_run_one = actions.add_parser("run-one", help="Run tests matching a substring")
    add_workspace_arg(p_run_one)
    p_run_one.add_argument("name_substr")

    p_list = actions.add_parser("list", help="Discover all test names")
    add_workspace_arg(p_list)

    p_logs = actions.add_parser("logs", help="Print raw output from the last run")
    add_workspace_arg(p_logs)

    p.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    workspace = resolve_workspace(args)
    runner = detect_runner(workspace)
    action = args.action

    if action == "run":
        result = runner.run_all(workspace)
        write_log(workspace, result.raw_output)
        emit_json(_result_payload(result))
        return EXIT_OK if result.failed == 0 else 1

    if action == "run-one":
        result = runner.run_one(workspace, args.name_substr)
        write_log(workspace, result.raw_output)
        emit_json(_result_payload(result))
        return EXIT_OK if result.failed == 0 and result.passed > 0 else 1

    if action == "list":
        emit_json(runner.list_tests(workspace))
        return EXIT_OK

    if action == "logs":
        emit_raw(read_log(workspace))
        return EXIT_OK

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)


def _result_payload(result) -> dict:
    return {
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
