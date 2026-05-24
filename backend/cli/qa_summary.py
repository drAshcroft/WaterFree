"""`waterfree qa-summary ...` — Ollama-powered QA over a file or URL."""

from __future__ import annotations

from argparse import Namespace, _SubParsersAction

from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    emit_error,
    emit_json,
)
from backend.qa_summary.core import OllamaUnavailable, run_qa_summary


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("qa-summary",
                       help="Ask a question about a file or URL using local Ollama")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_ask = actions.add_parser("ask", help="Run a QA summary")
    p_ask.add_argument("source", help="Local file path or HTTP(S) URL")
    p_ask.add_argument("-q", "--question", required=True)

    p.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    if args.action != "ask":
        return emit_error(f"unknown action: {args.action}", exit_code=EXIT_USAGE)

    try:
        result = run_qa_summary(args.source, args.question)
    except OllamaUnavailable as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)

    emit_json(result)
    return EXIT_OK
