import os
import shutil
import unittest
import uuid
from pathlib import Path

from backend.mcp_logging import configure_mcp_logger, resolve_mcp_log_dir

_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_mcp_logging_tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class MCPLoggingTests(unittest.TestCase):
    def make_temp_dir(self) -> Path:
        path = _TMP_ROOT / uuid.uuid4().hex
        path.mkdir(parents=True, exist_ok=False)
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        return path

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

    def test_resolve_mcp_log_dir_uses_appdata_when_present(self) -> None:
        tmp = self.make_temp_dir()
        original_explicit = os.environ.get("WATERFREE_MCP_LOG_DIR")
        original_appdata = os.environ.get("APPDATA")
        try:
            os.environ.pop("WATERFREE_MCP_LOG_DIR", None)
            os.environ["APPDATA"] = str(tmp)
            self.assertEqual(
                resolve_mcp_log_dir(),
                tmp / "WaterFree" / "logs" / "mcp",
            )
        finally:
            if original_explicit is None:
                os.environ.pop("WATERFREE_MCP_LOG_DIR", None)
            else:
                os.environ["WATERFREE_MCP_LOG_DIR"] = original_explicit
            if original_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = original_appdata

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


if __name__ == "__main__":
    unittest.main()
