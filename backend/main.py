"""
Unified entry point — used by the PyInstaller-built executable.

Usage:
    waterfree serve             VS Code bridge server
    waterfree mcp <mode>        MCP server  (index | knowledge | todos |
                                             debug | testing | qa-summary)
"""
from __future__ import annotations

import sys

_MCP_MODES = ("index", "knowledge", "todos", "debug", "testing", "qa-summary")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        _usage()

    command = args[0]

    if command == "serve":
        from backend.server import run
        run()

    elif command == "mcp":
        if len(args) < 2 or args[1] not in _MCP_MODES:
            _usage()
        _run_mcp(args[1])

    else:
        _usage()


def _run_mcp(mode: str) -> None:
    if mode == "index":
        from backend.mcp_index import mcp
    elif mode == "knowledge":
        from backend.mcp_knowledge import mcp
    elif mode == "todos":
        from backend.mcp_todos import mcp
    elif mode == "debug":
        from backend.mcp_debug import mcp
    elif mode == "testing":
        from backend.mcp_testing import mcp
    elif mode == "qa-summary":
        from backend.mcp_qa_summary import mcp
    else:
        _usage()
        return
    mcp.run()


def _usage() -> None:
    modes = " | ".join(_MCP_MODES)
    print(f"Usage: waterfree <serve | mcp <{modes}>>", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
