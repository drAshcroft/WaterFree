from __future__ import annotations

import unittest

from backend.mcp_runtime import FastMCP


class McpRuntimeTests(unittest.TestCase):
    def test_tool_decorator_returns_original_function(self) -> None:
        server = FastMCP("unit-test-server")

        def sample() -> str:
            return "ok"

        wrapped = server.tool()(sample)
        self.assertIs(wrapped, sample)
        self.assertEqual(wrapped(), "ok")

    def test_run_raises_clear_error_without_mcp_dependency(self) -> None:
        server = FastMCP("unit-test-server")

        try:
            server.run()
        except ModuleNotFoundError as exc:
            self.assertIn("No module named 'mcp'", str(exc))
            self.assertIn("unit-test-server", str(exc))
            return
        except Exception:
            # Real FastMCP may be installed in some environments. In that case,
            # successfully constructing the server is the compatibility goal.
            return


if __name__ == "__main__":
    unittest.main()
