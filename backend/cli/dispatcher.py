"""Top-level CLI dispatch — wires each area's subparser together."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Sequence

from backend.cli import imagegen as area_imagegen
from backend.cli import index as area_index
from backend.cli import knowledge as area_knowledge
from backend.cli import qa as area_qa
from backend.cli import qa_summary as area_qa_summary
from backend.cli import testing as area_testing
from backend.cli import todos as area_todos
from backend.cli import usage as area_usage
from backend.cli import usage_log
from backend.cli import vision as area_vision
from backend.cli import writing_grade as area_writing_grade
from backend.cli._common import EXIT_INTERNAL, EXIT_USAGE, take_last_output
from backend.cli.areas import CLI_AREAS  # noqa: F401 -- re-exported for main.py's gate


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waterfree",
        description="WaterFree workspace toolkit. See docs/cli-surface.md.",
    )
    sub = parser.add_subparsers(dest="area", metavar="<area>")
    sub.required = True

    area_todos.register(sub)
    area_knowledge.register(sub)
    area_index.register(sub)
    area_testing.register(sub)
    area_qa.register(sub)
    area_qa_summary.register(sub)
    area_writing_grade.register(sub)
    area_vision.register(sub)
    area_imagegen.register(sub)
    area_usage.register(sub)

    return parser


def dispatch(argv: Sequence[str]) -> int:
    parser = _build_parser()
    argv = list(argv)
    args = parser.parse_args(argv)
    runner = getattr(args, "_runner", None)
    if runner is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    # Every action is timed and logged (see usage_log). The log never raises and
    # never changes the exit code; `waterfree usage ...` itself is not logged so
    # reading the log does not pollute it.
    take_last_output()
    started = time.perf_counter()
    exit_code = EXIT_INTERNAL
    try:
        exit_code = runner(args)
        return exit_code
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else EXIT_INTERNAL
        raise
    finally:
        if args.area != "usage":
            _log_invocation(args, argv, exit_code, (time.perf_counter() - started) * 1000.0)


def _log_invocation(args: argparse.Namespace, argv: list[str], exit_code: int, duration_ms: float) -> None:
    try:
        result_bytes, payload = take_last_output()
        rest = argv[2:] if len(argv) >= 2 else []
        workspace = getattr(args, "workspace", None)
        if workspace is None and args.area in ("todos", "index", "testing", "qa"):
            import os
            workspace = os.getcwd()
        record = usage_log.build_record(
            area=str(args.area),
            action=str(getattr(args, "action", "")),
            argv=rest,
            workspace=str(workspace or ""),
            exit_code=int(exit_code),
            duration_ms=duration_ms,
            result_bytes=result_bytes,
            payload=payload,
        )
        usage_log.append(record)
    except Exception:  # pragma: no cover - logging must never break a command
        pass


def main(argv: Sequence[str] | None = None) -> int:
    return dispatch(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
