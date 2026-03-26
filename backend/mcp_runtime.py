"""
Compatibility helpers for optional MCP server runtime imports.

The VS Code backend and unit tests reuse helper functions from the `mcp_*`
modules directly. The external `mcp` package is only required when running
those modules as standalone MCP servers.
"""

from __future__ import annotations


class _FastMCPStub:
    def __init__(self, server_name: str):
        self._server_name = server_name

    def tool(self):
        def _decorator(func):
            return func

        return _decorator

    def run(self) -> None:
        raise ModuleNotFoundError(
            "No module named 'mcp'. Install backend requirements before running "
            f"the standalone MCP server '{self._server_name}'."
        )


try:
    from mcp.server.fastmcp import FastMCP as FastMCP  # type: ignore[no-redef]
except ModuleNotFoundError as exc:
    if exc.name != "mcp":
        raise
    FastMCP = _FastMCPStub
