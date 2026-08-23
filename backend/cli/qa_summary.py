"""`waterfree qa-summary ...` -- map/reduce QA over a large file or URL.

Runs against local Ollama by default. Point the `qa_summary` stage at an
OpenRouter provider in `.waterfree/providers.json` to route it remotely; the
key comes from $OPENROUTER_API_KEY since this CLI runs outside the extension
host and cannot read VS Code SecretStorage.
"""

from __future__ import annotations

import os
from argparse import Namespace, _SubParsersAction

from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    emit_error,
    emit_json,
)
from backend.llm.chat_client import ChatUnavailable
from backend.qa_summary.core import run_qa_summary


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("qa-summary",
                       help="Ask a question about a large file or URL")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_ask = actions.add_parser("ask", help="Run a QA summary")
    p_ask.add_argument("source", help="Local file path or HTTP(S) URL")
    p_ask.add_argument("-q", "--question", required=True)
    p_ask.add_argument(
        "--workspace",
        default=".",
        help="Project root holding .waterfree/providers.json, which selects the "
             "model for the qa_summary stage. Defaults to the current directory.",
    )

    p.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    if args.action != "ask":
        return emit_error(f"unknown action: {args.action}", exit_code=EXIT_USAGE)

    try:
        result = run_qa_summary(
            args.source,
            args.question,
            workspace_path=os.path.abspath(getattr(args, "workspace", ".") or "."),
        )
    except ChatUnavailable as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)

    emit_json(result)
    return EXIT_OK
