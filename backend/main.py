"""
Unified entry point — used by the PyInstaller-built executable.

Usage:
    waterfree serve                  VS Code bridge server
    waterfree doctor                 Verify bundled runtime dependencies
    waterfree <area> <action> ...    CLI subcommand
                                     (see docs/cli-surface.md)

Areas: todos | knowledge | index | testing | qa-summary
"""
from __future__ import annotations

import multiprocessing
import sys

_CLI_AREAS = ("todos", "knowledge", "index", "testing", "qa-summary")


def main() -> None:
    multiprocessing.freeze_support()

    args = sys.argv[1:]
    if not args:
        _usage()

    command = args[0]

    if command in ("-h", "--help"):
        _usage(exit_code=0)

    if command == "serve":
        from backend.server import run
        run()
        return

    if command == "doctor":
        from backend.diagnostics import run_doctor
        sys.exit(run_doctor(args[1:]))

    if command in _CLI_AREAS:
        from backend.cli.dispatcher import dispatch
        sys.exit(dispatch(args))

    _usage()


def _usage(exit_code: int = 1) -> None:
    areas = " | ".join(_CLI_AREAS)
    print(
        "Usage:\n"
        "  waterfree serve\n"
        f"  waterfree <{areas}> <action> [flags]\n"
        "\n"
        "Run `waterfree <area> --help` for area-specific options.",
        file=sys.stderr,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
