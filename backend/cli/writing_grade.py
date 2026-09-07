"""`waterfree writing-grade grade <file>` -- creative-writing evaluation."""

from __future__ import annotations

import os
from argparse import Namespace, _SubParsersAction

from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_INTERNAL,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    emit_error,
    emit_json,
)
from backend.llm.chat_client import ChatUnavailable
from backend.writing_grade.core import GradeResponseError, grade_writing


def register(sub: _SubParsersAction) -> None:
    parser = sub.add_parser("writing-grade", help="Grade a creative-writing file")
    actions = parser.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    grade = actions.add_parser("grade", help="Return six 0-20 writing scores as JSON")
    grade.add_argument("source", help="Local text file to grade")
    grade.add_argument(
        "--workspace",
        default=".",
        help="Project root holding .waterfree/providers.json. Defaults to CWD.",
    )
    parser.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    if args.action != "grade":
        return emit_error(f"unknown action: {args.action}", exit_code=EXIT_USAGE)
    try:
        result = grade_writing(
            args.source,
            workspace_path=os.path.abspath(getattr(args, "workspace", ".") or "."),
        )
    except FileNotFoundError as exc:
        return emit_error(str(exc), exit_code=EXIT_NOT_FOUND)
    except ChatUnavailable as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)
    except GradeResponseError as exc:
        return emit_error(str(exc), exit_code=EXIT_INTERNAL)

    emit_json(result)
    return EXIT_OK
