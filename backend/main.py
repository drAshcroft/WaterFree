"""
Unified entry point — used by the PyInstaller-built executable.

Usage:
    waterfree serve                  VS Code bridge server
    waterfree doctor                 Verify bundled runtime dependencies
    waterfree <area> <action> ...    CLI subcommand
                                     (see docs/cli-surface.md)

Areas: see backend/cli/areas.py (the single source of truth).
"""
from __future__ import annotations

import multiprocessing
import sys

# Before anything imports a third-party package: drop the venv root and a
# foreign cwd from sys.path so scratch scripts left there cannot shadow real
# packages (IMG-SYSPATH-001).
from backend.runtime_paths import harden_sys_path

harden_sys_path()

# Imported rather than restated: this module and the dispatcher previously kept
# separate copies, and a new area registered in one was invisible to the other.
from backend.cli.areas import CLI_AREAS as _CLI_AREAS


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

    if args[:2] == ["knowledge", "hook-pretool"]:
        # Fires before every Bash tool call: skip the dispatcher's eager area
        # imports (~0.7 s) and never exit non-zero (exit 2 blocks the call).
        from backend.cli.knowledge_pretool import main as pretool_main
        sys.exit(pretool_main(args[2:]))

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
        "Run `waterfree <area> --help` for area-specific options.\n"
        "Owned asset and prop lookup: use the waterfree-assets skill's assetsearch.py.\n"
        "See docs/cli-surface.md for the catalog entry point.",
        file=sys.stderr,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
