"""
Workspace-bounded testing tool descriptors.
"""

from __future__ import annotations

from pathlib import Path

from .types import ToolDescriptor, ToolPolicy


def _log_path(workspace_path: str) -> Path:
    root = Path(workspace_path).resolve() / ".waterfree" / "testing"
    root.mkdir(parents=True, exist_ok=True)
    return root / "last_run.log"


def _write_log(workspace_path: str, raw_output: str) -> None:
    _log_path(workspace_path).write_text(raw_output, encoding="utf-8")


def _read_log(workspace_path: str) -> str:
    path = _log_path(workspace_path)
    if not path.exists():
        return "No test logs found. Run tests first."
    return path.read_text(encoding="utf-8")


def _result_dict(result) -> dict:
    return {
        "passed": int(getattr(result, "passed", 0) or 0),
        "failed": int(getattr(result, "failed", 0) or 0),
        "results": [
            {
                "name": getattr(item, "name", ""),
                "passed": bool(getattr(item, "passed", False)),
                "error": getattr(item, "error", None),
                "durationMs": getattr(item, "duration_ms", None),
            }
            for item in list(getattr(result, "results", []) or [])
        ],
    }


def _summary(result) -> str:
    passed = int(getattr(result, "passed", 0) or 0)
    failed = int(getattr(result, "failed", 0) or 0)
    total = passed + failed
    if failed == 0:
        return f"Yes: {passed}"
    failing = [
        getattr(item, "name", "")
        for item in list(getattr(result, "results", []) or [])
        if not bool(getattr(item, "passed", False))
    ]
    if failing:
        return f"Failing tests: {failed}/{total}\n" + "\n".join(failing)
    return f"Failing tests: {failed}/{total}"


def testing_tool_descriptors() -> list[ToolDescriptor]:
    def list_tests(_args: dict, workspace_path: str) -> dict:
        from backend.mcp_testing import detect_runner

        tests = detect_runner(workspace_path).list_tests(workspace_path)
        return {"tests": tests, "count": len(tests)}

    def run_tests(_args: dict, workspace_path: str) -> dict:
        from backend.mcp_testing import detect_runner

        result = detect_runner(workspace_path).run_all(workspace_path)
        _write_log(workspace_path, getattr(result, "raw_output", ""))
        payload = _result_dict(result)
        payload["summary"] = _summary(result)
        return payload

    def run_test(args: dict, workspace_path: str) -> dict:
        from backend.mcp_testing import detect_runner

        test_name = str(args.get("testName", ""))
        result = detect_runner(workspace_path).run_one(workspace_path, test_name)
        _write_log(workspace_path, getattr(result, "raw_output", ""))
        payload = _result_dict(result)
        payload["summary"] = "Yes" if payload["failed"] == 0 and payload["passed"] > 0 else _summary(result)
        payload["testName"] = test_name
        return payload

    def get_test_logs(_args: dict, workspace_path: str) -> dict:
        return {"logs": _read_log(workspace_path)}

    return [
        ToolDescriptor(
            name="list_tests",
            title="list tests",
            description="Discover and list test names available in the workspace.",
            input_schema={"type": "object", "properties": {}},
            handler=list_tests,
            policy=ToolPolicy(read_only=True, category="testing"),
            server_id="waterfree-testing",
        ),
        ToolDescriptor(
            name="run_tests",
            title="run tests",
            description="Run the workspace test suite and return a concise result summary.",
            input_schema={"type": "object", "properties": {}},
            handler=run_tests,
            policy=ToolPolicy(read_only=False, category="testing"),
            server_id="waterfree-testing",
        ),
        ToolDescriptor(
            name="run_test",
            title="run test",
            description="Run a specific test by substring match and return a concise result summary.",
            input_schema={
                "type": "object",
                "properties": {"testName": {"type": "string"}},
                "required": ["testName"],
            },
            handler=run_test,
            policy=ToolPolicy(read_only=False, category="testing"),
            server_id="waterfree-testing",
        ),
        ToolDescriptor(
            name="get_test_logs",
            title="get test logs",
            description="Read the raw output from the last workspace test run.",
            input_schema={"type": "object", "properties": {}},
            handler=get_test_logs,
            policy=ToolPolicy(read_only=True, category="testing"),
            server_id="waterfree-testing",
        ),
    ]
