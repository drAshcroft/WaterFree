import json
import os
import unittest
import uuid
from pathlib import Path
from unittest import mock

from backend.mcp_logging import configure_mcp_logger, instrument_tool, resolve_mcp_log_dir
from backend.test_support import make_temp_dir as make_test_dir


class MCPLoggingTests(unittest.TestCase):
    def make_temp_dir(self) -> Path:
        return make_test_dir(self, prefix="mcp-logging-")

    def test_resolve_mcp_log_dir_prefers_explicit_env(self) -> None:
        tmp = self.make_temp_dir()
        original = os.environ.get("WATERFREE_MCP_LOG_DIR")
        try:
            os.environ["WATERFREE_MCP_LOG_DIR"] = str(tmp)
            self.assertEqual(resolve_mcp_log_dir(), tmp)
        finally:
            if original is None:
                os.environ.pop("WATERFREE_MCP_LOG_DIR", None)
            else:
                os.environ["WATERFREE_MCP_LOG_DIR"] = original

    def test_resolve_mcp_log_dir_defaults_to_project_waterfree_dir(self) -> None:
        tmp = self.make_temp_dir()
        project_root = tmp / "repo"
        nested_dir = project_root / "src" / "nested"
        (project_root / ".git").mkdir(parents=True, exist_ok=True)
        nested_dir.mkdir(parents=True, exist_ok=True)
        original_explicit = os.environ.get("WATERFREE_MCP_LOG_DIR")
        original_cwd = Path.cwd()
        try:
            os.environ.pop("WATERFREE_MCP_LOG_DIR", None)
            os.chdir(nested_dir)
            self.assertEqual(
                resolve_mcp_log_dir(),
                project_root / ".waterfree" / "logs" / "mcp",
            )
        finally:
            os.chdir(original_cwd)
            if original_explicit is None:
                os.environ.pop("WATERFREE_MCP_LOG_DIR", None)
            else:
                os.environ["WATERFREE_MCP_LOG_DIR"] = original_explicit

    def test_configure_mcp_logger_creates_server_log_file(self) -> None:
        tmp = self.make_temp_dir()
        original = os.environ.get("WATERFREE_MCP_LOG_DIR")
        try:
            os.environ["WATERFREE_MCP_LOG_DIR"] = str(tmp)
            server_name = f"unit-test-server-{uuid.uuid4().hex}"
            logger, log_file = configure_mcp_logger(server_name)
            logger.info("hello")
            self.assertEqual(log_file, tmp / f"{server_name}.log")
            self.assertTrue(log_file.exists())
            self.assertIn("hello", log_file.read_text(encoding="utf-8"))
        finally:
            if original is None:
                os.environ.pop("WATERFREE_MCP_LOG_DIR", None)
            else:
                os.environ["WATERFREE_MCP_LOG_DIR"] = original
            if "logger" in locals():
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

    def test_configure_mcp_logger_falls_back_to_stream_handler(self) -> None:
        tmp = self.make_temp_dir()
        original = os.environ.get("WATERFREE_MCP_LOG_DIR")
        try:
            os.environ["WATERFREE_MCP_LOG_DIR"] = str(tmp)
            server_name = f"unit-test-fallback-{uuid.uuid4().hex}"
            with mock.patch("backend.mcp_logging.logging.FileHandler", side_effect=PermissionError("blocked")):
                logger, log_file = configure_mcp_logger(server_name)
            logger.info("stderr-only")
            self.assertEqual(log_file, tmp / f"{server_name}.log")
            self.assertEqual(len(logger.handlers), 1)
        finally:
            if original is None:
                os.environ.pop("WATERFREE_MCP_LOG_DIR", None)
            else:
                os.environ["WATERFREE_MCP_LOG_DIR"] = original
            if "logger" in locals():
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

    def test_instrument_tool_returns_json_error_payload(self) -> None:
        tmp = self.make_temp_dir()
        original = os.environ.get("WATERFREE_MCP_LOG_DIR")
        try:
            os.environ["WATERFREE_MCP_LOG_DIR"] = str(tmp)
            logger, _ = configure_mcp_logger(f"unit-test-tool-error-{uuid.uuid4().hex}")

            def fail_tool() -> str:
                raise RuntimeError("boom")

            wrapped = instrument_tool(logger, "fail_tool", fail_tool)
            payload = json.loads(wrapped())
            self.assertEqual(payload["tool"], "fail_tool")
            self.assertEqual(payload["errorType"], "RuntimeError")
            self.assertEqual(payload["error"], "boom")
        finally:
            if original is None:
                os.environ.pop("WATERFREE_MCP_LOG_DIR", None)
            else:
                os.environ["WATERFREE_MCP_LOG_DIR"] = original
            if "logger" in locals():
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
