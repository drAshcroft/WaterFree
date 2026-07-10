"""Shared helpers for the CLI subcommands."""

from __future__ import annotations

import json
import os
import sys
from argparse import ArgumentParser, Namespace
from typing import Any

# Exit codes — see docs/cli-surface.md
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_DEP_MISSING = 4


def add_workspace_arg(parser: ArgumentParser, *, required: bool = False) -> None:
    """Add the standard --workspace flag. Defaults to CWD when not required."""
    parser.add_argument(
        "--workspace",
        default=None,
        help="Path to the project root. Defaults to current working directory.",
        required=required,
    )


def add_full_arg(parser: ArgumentParser) -> None:
    """Add the standard --full flag for commands with compact output modes."""
    parser.add_argument(
        "--full",
        action="store_true",
        help="Emit every field when the command supports compact output.",
    )


def resolve_workspace(args: Namespace) -> str:
    ws = getattr(args, "workspace", None) or os.getcwd()
    return os.path.abspath(ws)


def emit_json(obj: Any) -> None:
    """Print a JSON value to stdout, indented for human inspection."""
    sys.stdout.write(json.dumps(obj, indent=2))
    sys.stdout.write("\n")


def emit_raw(text: str) -> None:
    """Print plain text to stdout (for tools that return non-JSON, e.g. logs)."""
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def emit_error(message: str, *, code: str = "error", exit_code: int = EXIT_INTERNAL) -> int:
    sys.stderr.write(f"error: {message}\n")
    return exit_code


def parse_json_arg(raw: str, *, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"error: --{label} must be valid JSON: {exc}\n")
        raise SystemExit(EXIT_USAGE) from exc
