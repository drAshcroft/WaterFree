"""
Tests for external-tool resolution.

The bug these exist for: `subprocess.run(["npx", ...])` on Windows raises
`FileNotFoundError: [WinError 2]` because the launcher is `npx.CMD` and argv[0]
is not subject to PATHEXT. It escaped as a raw traceback three separate times
before anyone wrote it down. Nothing in a runner may put a bare command name in
an argv again.
"""

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.testing.errors import ToolchainError
from backend.testing import toolchain


class ResolveToolTests(unittest.TestCase):
    def test_returns_absolute_path_from_which(self) -> None:
        with mock.patch.object(toolchain.shutil, "which", return_value=r"C:\node\npx.CMD"):
            self.assertEqual(
                toolchain.resolve_tool("npx", install_hint="hint"),
                r"C:\node\npx.CMD",
            )

    def test_missing_tool_raises_with_install_hint(self) -> None:
        with mock.patch.object(toolchain.shutil, "which", return_value=None):
            with self.assertRaises(ToolchainError) as ctx:
                toolchain.resolve_tool("npx", install_hint="Install Node.js first.")
        self.assertIn("npx", str(ctx.exception))
        self.assertIn("Install Node.js first.", str(ctx.exception))


class LocalBinTests(unittest.TestCase):
    def test_finds_pinned_binary(self) -> None:
        with TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True)
            name = "vitest.cmd" if os.name == "nt" else "vitest"
            (bin_dir / name).write_text("", encoding="utf-8")
            self.assertEqual(
                toolchain.local_bin(tmp, "vitest"),
                str(bin_dir / name),
            )

    def test_returns_none_when_not_installed(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(toolchain.local_bin(tmp, "vitest"))


class NodeToolTests(unittest.TestCase):
    def test_prefers_the_projects_own_install(self) -> None:
        with mock.patch.object(toolchain, "local_bin", return_value=r"C:\ws\node_modules\.bin\vitest.cmd"):
            self.assertEqual(
                toolchain.node_tool("C:/ws", "vitest"),
                [r"C:\ws\node_modules\.bin\vitest.cmd"],
            )

    def test_falls_back_to_resolved_npx(self) -> None:
        with mock.patch.object(toolchain, "local_bin", return_value=None), \
             mock.patch.object(toolchain.shutil, "which", return_value=r"C:\node\npx.CMD"):
            self.assertEqual(
                toolchain.node_tool("C:/ws", "vitest"),
                [r"C:\node\npx.CMD", "vitest"],
            )

    def test_never_emits_a_bare_command_name(self) -> None:
        """The regression guard for WinError 2."""
        with mock.patch.object(toolchain, "local_bin", return_value=None), \
             mock.patch.object(toolchain.shutil, "which", return_value=r"C:\node\npx.CMD"):
            argv = toolchain.node_tool("C:/ws", "jest")
        self.assertNotEqual(argv[0], "npx")
        self.assertTrue(os.path.isabs(argv[0]) or os.sep in argv[0])

    def test_missing_npx_raises_instead_of_failing_at_spawn(self) -> None:
        with mock.patch.object(toolchain, "local_bin", return_value=None), \
             mock.patch.object(toolchain.shutil, "which", return_value=None):
            with self.assertRaises(ToolchainError):
                toolchain.node_tool("C:/ws", "jest")


class FilterArgTests(unittest.TestCase):
    def test_accepts_an_ordinary_test_name(self) -> None:
        self.assertEqual(
            toolchain.check_filter_arg("renders the board correctly"),
            "renders the board correctly",
        )

    def test_rejects_cmd_metacharacters(self) -> None:
        for value in ("a & echo hi", "a | b", "a > out.txt", "a%PATH%b"):
            with self.subTest(value=value):
                with self.assertRaises(ToolchainError):
                    toolchain.check_filter_arg(value)


class RunToolTests(unittest.TestCase):
    def test_translates_spawn_failure_into_toolchain_error(self) -> None:
        with mock.patch.object(
            toolchain.subprocess, "run",
            side_effect=FileNotFoundError(2, "The system cannot find the file specified"),
        ):
            with self.assertRaises(ToolchainError) as ctx:
                toolchain.run_tool(["npx", "vitest"], workspace_path=".", timeout=5)
        self.assertIn("Could not launch", str(ctx.exception))

    def test_timeout_is_not_swallowed(self) -> None:
        """Callers distinguish a slow suite from a broken toolchain."""
        with mock.patch.object(
            toolchain.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=5),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                toolchain.run_tool(["x"], workspace_path=".", timeout=5)


if __name__ == "__main__":
    unittest.main()
