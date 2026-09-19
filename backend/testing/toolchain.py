"""
Locating and launching the external tools the JS / .NET runners depend on.

Everything here exists because of one Windows detail that cost this project
three separate knowledge-base entries: node ships `npx.CMD`, not `npx`, and
`subprocess.run(["npx", ...])` does not consult PATHEXT. The call raises
`FileNotFoundError: [WinError 2]` before the test framework is ever reached, so
the user sees a Python traceback where a test report should be.

`shutil.which` *does* consult PATHEXT, so resolving to an absolute path first
fixes it on Windows and changes nothing on POSIX. We prefer the workspace's own
`node_modules/.bin/` entry when it exists: it is the version the project pinned,
and it skips npx's resolution step entirely.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from backend.testing.errors import ToolchainError

# Characters cmd.exe treats specially. Python 3.12+ quotes arguments correctly
# when the target is a .bat/.cmd, so this is defence in depth rather than the
# only guard — but test-name filters come straight from a CLI argument, and a
# runner is not the place to find out that the interpreter's escaping changed.
_CMD_METACHARACTERS = set('&|<>^"%\r\n')

NODE_INSTALL_HINT = (
    "Install Node.js (https://nodejs.org) and make sure `npm` is on PATH, "
    "then run `npm install` in the project."
)

DOTNET_INSTALL_HINT = (
    "Install the .NET SDK (https://dotnet.microsoft.com/download) and make "
    "sure `dotnet` is on PATH."
)


def resolve_tool(name: str, *, install_hint: str) -> str:
    """Return an absolute path to `name`, or raise ToolchainError.

    Always go through this instead of putting a bare command name in an argv —
    see the module docstring for the Windows failure it prevents.
    """
    found = shutil.which(name)
    if not found:
        raise ToolchainError(f"`{name}` was not found on PATH. {install_hint}")
    return found


def local_bin(workspace_path: str, name: str) -> str | None:
    """Path to `node_modules/.bin/<name>` for this workspace, if installed.

    On Windows the launcher we want is `<name>.cmd`; the extensionless sibling
    is a shell script that CreateProcess cannot execute.
    """
    bin_dir = Path(workspace_path) / "node_modules" / ".bin"
    candidates = [f"{name}.cmd", name] if os.name == "nt" else [name]
    for candidate in candidates:
        path = bin_dir / candidate
        if path.is_file():
            return str(path)
    return None


def node_tool(workspace_path: str, name: str) -> list[str]:
    """argv prefix that runs the JS tool `name` for this workspace.

    The project's own install wins; otherwise fall back to `npx <name>`, which
    is what a developer would type. Raises ToolchainError when neither the
    local binary nor npx exists, rather than failing at spawn time.
    """
    pinned = local_bin(workspace_path, name)
    if pinned:
        return [pinned]
    return [resolve_tool("npx", install_hint=NODE_INSTALL_HINT), name]


def npm(workspace_path: str) -> str:
    """Absolute path to the `npm` launcher."""
    return resolve_tool("npm", install_hint=NODE_INSTALL_HINT)


def dotnet() -> str:
    """Absolute path to the `dotnet` CLI."""
    return resolve_tool("dotnet", install_hint=DOTNET_INSTALL_HINT)


def check_filter_arg(value: str, *, label: str = "test name filter") -> str:
    """Reject a user-supplied filter that could be reinterpreted by cmd.exe.

    Substring filters are forwarded to tools whose Windows launchers are batch
    files. Refusing the handful of characters no real test name contains is
    cheaper than reasoning about every launcher's quoting.
    """
    bad = sorted(_CMD_METACHARACTERS & set(value))
    if bad:
        raise ToolchainError(
            f"The {label} contains characters that cannot be passed safely to "
            f"the test runner: {' '.join(repr(c) for c in bad)}. "
            "Use a plain substring of the test name."
        )
    return value


def run_tool(
    cmd: list[str],
    *,
    workspace_path: str,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a resolved command, translating spawn failures into ToolchainError.

    `cmd[0]` is expected to have come from `resolve_tool` / `node_tool`; the
    OSError branch catches the residual cases (a pinned binary deleted between
    the lookup and the call, a broken shim) so callers never see WinError 2.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **env} if env else None,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise ToolchainError(f"Could not launch `{cmd[0]}`: {exc}") from exc
