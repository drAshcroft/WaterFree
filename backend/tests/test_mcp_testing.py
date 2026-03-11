import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from backend.mcp_testing import UnittestRunner, _parse_unittest_output, _run_command


WORKSPACE = Path(__file__).resolve().parents[2]


class McpTestingRunnerTests(unittest.TestCase):
    def test_parse_unittest_output_handles_interleaved_logs(self) -> None:
        raw = """
test_configure_mcp_logger_creates_server_log_file (test_mcp_logging.MCPLoggingTests.test_configure_mcp_logger_creates_server_log_file) ... 2026-03-09 08:36:05,330 [INFO] waterfree.mcp.unit-test-server: hello
ok
test_plain_pass (pkg.Class.test_plain_pass) ... ok
"""
        result = _parse_unittest_output(raw)

        self.assertEqual(result.failed, 0)
        self.assertEqual(result.passed, 2)
        self.assertEqual(
            [test.name for test in result.results],
            [
                "test_configure_mcp_logger_creates_server_log_file (test_mcp_logging.MCPLoggingTests.test_configure_mcp_logger_creates_server_log_file)",
                "test_plain_pass (pkg.Class.test_plain_pass)",
            ],
        )

    def test_run_command_uses_devnull_stdin(self) -> None:
        with mock.patch("backend.mcp_testing.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess([], 0, "", "")

            _run_command(["python", "-V"], workspace_path=str(WORKSPACE), timeout=12)

        _, kwargs = run_mock.call_args
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], str(WORKSPACE))
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["timeout"], 12)

    def test_list_tests_survives_piped_parent_stdin(self) -> None:
        script = (
            "import json; "
            "from backend.mcp_testing import UnittestRunner; "
            f"names = UnittestRunner().list_tests(r'{WORKSPACE}'); "
            "print(json.dumps({'count': len(names), 'first': names[0] if names else ''}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=WORKSPACE,
            input="",
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertGreater(payload["count"], 0)
        self.assertIn("test_", payload["first"])

    def test_run_one_survives_piped_parent_stdin(self) -> None:
        script = (
            "import json; "
            "from backend.mcp_testing import UnittestRunner; "
            f"result = UnittestRunner().run_one(r'{WORKSPACE}', 'test_resolve_mcp_log_dir_prefers_explicit_env'); "
            "print(json.dumps({'passed': result.passed, 'failed': result.failed}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=WORKSPACE,
            input="",
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip())
        self.assertEqual(payload["failed"], 0)
        self.assertGreater(payload["passed"], 0)
