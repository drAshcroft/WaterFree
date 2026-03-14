"""
Workspace-bounded linter and static-check tool descriptors.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .types import ToolDescriptor, ToolPolicy


def _log_path(workspace_path: str) -> Path:
    root = Path(workspace_path).resolve() / ".waterfree" / "linting"
    root.mkdir(parents=True, exist_ok=True)
    return root / "last_run.log"


def _write_log(workspace_path: str, raw_output: str) -> None:
    _log_path(workspace_path).write_text(raw_output, encoding="utf-8")


def _read_log(workspace_path: str) -> str:
    path = _log_path(workspace_path)
    if not path.exists():
        return "No lint logs found. Run a linter first."
    return path.read_text(encoding="utf-8")


def _package_json_scripts(workspace: Path) -> dict[str, str]:
    package_json = workspace / "package.json"
    if not package_json.exists():
        return {}
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    scripts = data.get("scripts", {})
    if not isinstance(scripts, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in scripts.items()
        if str(key).strip() and str(value).strip()
    }


def _available_linters(workspace_path: str) -> list[dict[str, object]]:
    workspace = Path(workspace_path).resolve()
    scripts = _package_json_scripts(workspace)
    linters: list[dict[str, object]] = []

    if "lint" in scripts:
        linters.append({
            "id": "npm_lint",
            "title": "npm run lint",
            "description": "Run the workspace lint script from package.json.",
            "command": ["npm", "run", "lint"],
        })

    local_tsc = workspace / "node_modules" / "typescript" / "bin" / "tsc"
    if (workspace / "tsconfig.json").exists() and local_tsc.exists():
        linters.append({
            "id": "tsc_no_emit",
            "title": "TypeScript check",
            "description": "Run the TypeScript compiler in no-emit mode.",
            "command": ["node", "node_modules/typescript/bin/tsc", "--noEmit"],
        })

    return linters


def _resolve_linter(workspace_path: str, linter_id: str) -> dict[str, object] | None:
    available = _available_linters(workspace_path)
    if not available:
        return None
    if not linter_id:
        return available[0]
    for item in available:
        if item["id"] == linter_id:
            return item
    return None


def lint_tool_descriptors() -> list[ToolDescriptor]:
    def list_linters(_args: dict, workspace_path: str) -> dict:
        linters = _available_linters(workspace_path)
        return {
            "linters": [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "description": item["description"],
                }
                for item in linters
            ],
            "count": len(linters),
        }

    def run_linter(args: dict, workspace_path: str) -> dict:
        linter_id = str(args.get("linterId", "") or "")
        linter = _resolve_linter(workspace_path, linter_id)
        if linter is None:
            available = _available_linters(workspace_path)
            return {
                "passed": False,
                "summary": "No matching linter found for this workspace.",
                "available": [item["id"] for item in available],
            }

        result = subprocess.run(
            list(linter["command"]),
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        raw_output = (result.stdout or "") + (result.stderr or "")
        _write_log(workspace_path, raw_output)
        passed = result.returncode == 0
        return {
            "passed": passed,
            "linterId": linter["id"],
            "title": linter["title"],
            "summary": "Yes" if passed else f"Failed: {linter['title']}",
            "exitCode": result.returncode,
        }

    def get_lint_logs(_args: dict, workspace_path: str) -> dict:
        return {"logs": _read_log(workspace_path)}

    return [
        ToolDescriptor(
            name="list_linters",
            title="list linters",
            description="List available linters and static checks for the current workspace.",
            input_schema={"type": "object", "properties": {}},
            handler=list_linters,
            policy=ToolPolicy(read_only=True, category="lint"),
            server_id="waterfree-lint",
        ),
        ToolDescriptor(
            name="run_linter",
            title="run linter",
            description="Run a workspace linter or compiler-style static check and return a concise summary.",
            input_schema={
                "type": "object",
                "properties": {"linterId": {"type": "string"}},
            },
            handler=run_linter,
            policy=ToolPolicy(read_only=True, category="lint"),
            server_id="waterfree-lint",
        ),
        ToolDescriptor(
            name="get_lint_logs",
            title="lint logs",
            description="Read the raw output from the last lint or static-check run.",
            input_schema={"type": "object", "properties": {}},
            handler=get_lint_logs,
            policy=ToolPolicy(read_only=True, category="lint"),
            server_id="waterfree-lint",
        ),
    ]
