"""Top-level CLI dispatch — wires each area's subparser together."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from backend.cli import index as area_index
from backend.cli import knowledge as area_knowledge
from backend.cli import qa_summary as area_qa_summary
from backend.cli import testing as area_testing
from backend.cli import todos as area_todos
from backend.cli._common import EXIT_USAGE


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
    area_qa_summary.register(sub)

    return parser


def dispatch(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    runner = getattr(args, "_runner", None)
    if runner is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    return runner(args)


def main(argv: Sequence[str] | None = None) -> int:
    return dispatch(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
