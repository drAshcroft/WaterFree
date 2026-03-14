"""
Workspace-bounded live debug tool descriptors.
"""

from __future__ import annotations

import json

from backend import mcp_debug

from .types import ToolDescriptor, ToolPolicy


def _parse_json(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"raw": raw}


def debug_tool_descriptors() -> list[ToolDescriptor]:
    def debug_status(_args: dict, workspace_path: str) -> dict:
        return _parse_json(mcp_debug._debug_status_impl(workspace_path))

    def get_execution_context(_args: dict, workspace_path: str) -> dict:
        return _parse_json(mcp_debug._get_execution_context_impl(workspace_path))

    def list_variables(args: dict, workspace_path: str) -> dict:
        scope = str(args.get("scope", "") or "")
        return _parse_json(mcp_debug._list_variables_impl(workspace_path, scope=scope))

    def get_variable_schema(args: dict, workspace_path: str) -> dict:
        var_name = str(args.get("varName", "") or "")
        scope = str(args.get("scope", "locals") or "locals")
        return _parse_json(
            mcp_debug._get_variable_schema_impl(workspace_path, var_name=var_name, scope=scope)
        )

    def get_variable_value(args: dict, workspace_path: str) -> dict:
        var_name = str(args.get("varName", "") or "")
        scope = str(args.get("scope", "locals") or "locals")
        path = str(args.get("path", "") or "")
        start = int(args.get("start", 0) or 0)
        end = int(args.get("end", 50) or 50)
        return _parse_json(
            mcp_debug._get_variable_value_impl(
                workspace_path,
                var_name=var_name,
                scope=scope,
                path=path,
                start=start,
                end=end,
            )
        )

    def debug_eval(args: dict, workspace_path: str) -> dict:
        expression = str(args.get("expression", "") or "")
        frame_id = int(args.get("frameId", 0) or 0)
        return _parse_json(
            mcp_debug._debug_eval_impl(workspace_path, expression=expression, frame_id=frame_id)
        )

    return [
        ToolDescriptor(
            name="debug_status",
            title="debug status",
            description="Check whether a debug session is active and whether live debug tools are available.",
            input_schema={"type": "object", "properties": {}},
            handler=debug_status,
            policy=ToolPolicy(read_only=True, category="debug"),
            server_id="waterfree-debug",
        ),
        ToolDescriptor(
            name="get_execution_context",
            title="execution context",
            description="Read the current breakpoint location, call stack, and code snippet around the stop point.",
            input_schema={"type": "object", "properties": {}},
            handler=get_execution_context,
            policy=ToolPolicy(read_only=True, category="debug"),
            server_id="waterfree-debug",
        ),
        ToolDescriptor(
            name="list_variables",
            title="list variables",
            description="List variable names and types from the current debug snapshot without reading their full values.",
            input_schema={
                "type": "object",
                "properties": {"scope": {"type": "string"}},
            },
            handler=list_variables,
            policy=ToolPolicy(read_only=True, category="debug"),
            server_id="waterfree-debug",
        ),
        ToolDescriptor(
            name="get_variable_schema",
            title="variable schema",
            description="Inspect the structure of a variable before fetching its full value.",
            input_schema={
                "type": "object",
                "properties": {
                    "varName": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "required": ["varName"],
            },
            handler=get_variable_schema,
            policy=ToolPolicy(read_only=True, category="debug"),
            server_id="waterfree-debug",
        ),
        ToolDescriptor(
            name="get_variable_value",
            title="variable value",
            description="Fetch a variable value or a sliced portion of it from the current debug snapshot.",
            input_schema={
                "type": "object",
                "properties": {
                    "varName": {"type": "string"},
                    "scope": {"type": "string"},
                    "path": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                "required": ["varName"],
            },
            handler=get_variable_value,
            policy=ToolPolicy(read_only=True, category="debug"),
            server_id="waterfree-debug",
        ),
        ToolDescriptor(
            name="debug_eval",
            title="debug repl",
            description="Evaluate an expression in the paused debugger REPL for the active stack frame.",
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                    "frameId": {"type": "integer"},
                },
                "required": ["expression"],
            },
            handler=debug_eval,
            policy=ToolPolicy(read_only=False, category="debug"),
            server_id="waterfree-debug",
        ),
    ]
