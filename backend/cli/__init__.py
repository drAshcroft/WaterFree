"""Command-line interface package — replaces the MCP stdio servers.

Each module under `backend.cli` exposes:
  * `register(subparsers)` — attaches its `<area>` subparser
  * `run(args) -> int`     — handles the parsed namespace, prints JSON to stdout,
                              progress/errors to stderr, returns an exit code

The top-level `dispatch(argv)` function in `backend.cli.dispatcher` wires the
five areas together. Exit codes follow `docs/cli-surface.md`.
"""
