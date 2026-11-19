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


# The dispatcher's usage log needs to know what an action wrote to stdout
# (size, and the envelope's total/returned/ids) without every area threading
# that back through its return value. emit_json/emit_raw note it here; the
# dispatcher collects it with `take_last_output()` after the runner returns.
_last_output: dict[str, Any] = {"bytes": 0, "payload": None}


def take_last_output() -> tuple[int, Any]:
    """(bytes written, last JSON payload) since the previous call; then reset."""
    out = (int(_last_output["bytes"]), _last_output["payload"])
    _last_output["bytes"] = 0
    _last_output["payload"] = None
    return out


def emit_json(obj: Any, *, compact: bool = False) -> None:
    """Print a JSON value to stdout, indented for human inspection.

    `compact=True` is for byte-budgeted outputs (receipts, titles-only rows):
    an envelope holding a list of row objects keeps one key per line and one
    minified line per row; anything else is printed on a single line.
    """
    text = _compact_json(obj) if compact else json.dumps(obj, indent=2)
    sys.stdout.write(text)
    sys.stdout.write("\n")
    _last_output["bytes"] += len(text) + 1
    _last_output["payload"] = obj


def _compact_json(obj: Any) -> str:
    def one(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"))

    def is_rows(value: Any) -> bool:
        return isinstance(value, list) and bool(value) and all(isinstance(item, dict) for item in value)

    if not (isinstance(obj, dict) and any(is_rows(v) for v in obj.values())):
        return one(obj)
    lines = []
    for key, value in obj.items():
        if is_rows(value):
            rows = ",\n".join(f"  {one(item)}" for item in value)
            lines.append(f"{json.dumps(key)}:[\n{rows}\n]")
        else:
            lines.append(f"{json.dumps(key)}:{one(value)}")
    return "{\n" + ",\n".join(lines) + "\n}"


def emit_raw(text: str) -> None:
    """Print plain text to stdout (for tools that return non-JSON, e.g. logs)."""
    sys.stdout.write(text)
    _last_output["bytes"] += len(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
        _last_output["bytes"] += 1


def emit_error(message: str, *, code: str = "error", exit_code: int = EXIT_INTERNAL) -> int:
    sys.stderr.write(f"error: {message}\n")
    return exit_code


def parse_json_arg(raw: str, *, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"error: --{label} must be valid JSON: {exc}\n")
        raise SystemExit(EXIT_USAGE) from exc
