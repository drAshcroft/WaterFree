"""
Live-debug tool implementations used by the in-process agent runtime.

These six callables (`_debug_status_impl`, `_get_execution_context_impl`,
`_list_variables_impl`, `_get_variable_schema_impl`, `_get_variable_value_impl`,
`_debug_eval_impl`) are consumed by `backend.llm.tools.debug_tools` to build
ToolDescriptors for the agent runtime that powers `waterfree serve`.

Previously this lived in `backend/mcp_debug.py` and was also exposed as a
standalone MCP stdio server. The MCP transport has been removed; the logic
stays here so the in-process agent (and the VS Code debug sidebar) keep
working.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from backend.debug.snapshot_reader import DebugSnapshot, VarInfo

_MAX_RESPONSE_CHARS = 2000
_EVAL_TIMEOUT_SECONDS = 8.0
_EVAL_POLL_INTERVAL = 0.15


# ----------------------------------------------------------------------------
# Tool implementations
# ----------------------------------------------------------------------------


def _debug_status_impl(workspace_path: str) -> str:
    """Check if a debug session is active in the workspace.

    Returns whether a snapshot or live state is available, when it was captured,
    the stop reason, and the current location. Call this first before any other
    debug tools.
    """
    try:
        snap = DebugSnapshot.load_latest(workspace_path)
        return json.dumps({
            "active": True,
            "hasSnapshot": True,
            "capturedAt": snap.captured_at,
            "stale": snap.is_stale(),
            "intent": snap.intent,
            "stopReason": snap.stop_reason,
            "location": {
                "file": snap.location.file,
                "line": snap.location.line,
                "qualifiedName": snap.location.qualified_name,
            },
            "scopeNames": snap.all_scope_names(),
            "evalAvailable": True,
        })
    except FileNotFoundError:
        pass

    live_path = Path(workspace_path) / ".waterfree" / "debug" / "live_state.json"
    if live_path.exists():
        try:
            data = json.loads(live_path.read_text(encoding="utf-8"))
            loc = data.get("location", {})
            return json.dumps({
                "active": True,
                "hasSnapshot": False,
                "capturedAt": data.get("capturedAt", ""),
                "stale": False,
                "intent": None,
                "stopReason": data.get("stopReason", "breakpoint"),
                "location": {
                    "file": loc.get("file", ""),
                    "line": loc.get("line", 0),
                    "qualifiedName": loc.get("qualifiedName", ""),
                },
                "scopeNames": [],
                "evalAvailable": True,
                "hint": "Auto-captured on breakpoint. Use debug_eval to inspect state. "
                        "For full variable list, ask user to click 'Push to Agent' in the WaterFree sidebar.",
                "exceptionMessage": data.get("exceptionMessage"),
            })
        except (json.JSONDecodeError, OSError):
            pass

    return json.dumps({"active": False, "reason": "No active debug session. Start debugging and pause at a breakpoint."})


def _get_execution_context_impl(workspace_path: str) -> str:
    """Get the full execution context at the breakpoint."""
    snap = DebugSnapshot.load_latest(workspace_path)
    code_snippet = _read_snippet(snap.location.file, snap.location.line, workspace_path)
    return json.dumps({
        "intent": snap.intent,
        "stopReason": snap.stop_reason,
        "capturedAt": snap.captured_at,
        "location": {
            "file": snap.location.file,
            "line": snap.location.line,
            "qualifiedName": snap.location.qualified_name,
        },
        "callStack": [
            {"frame": f.frame, "file": f.file, "line": f.line}
            for f in snap.call_stack
        ],
        "codeSnippet": code_snippet,
        "exceptionMessage": snap.exception_message,
    })


def _list_variables_impl(workspace_path: str, scope: str = "") -> str:
    """List variable names and types only — no values."""
    snap = DebugSnapshot.load_latest(workspace_path)
    if scope and scope in snap.scopes:
        scopes_to_show = {scope: snap.scopes[scope]}
    else:
        scopes_to_show = snap.scopes

    result: dict[str, list[dict]] = {}
    for scope_name, vars_list in scopes_to_show.items():
        result[scope_name] = [
            {
                "name": v.name,
                "type": v.type_name,
                "isExpandable": v.variables_reference > 0,
            }
            for v in vars_list
        ]
    return json.dumps({"scopes": result})


def _get_variable_schema_impl(workspace_path: str, var_name: str, scope: str = "locals") -> str:
    """Get the structure/schema of a variable without fetching all values."""
    snap = DebugSnapshot.load_latest(workspace_path)
    var = snap.find_var(var_name, scope)
    if var is None:
        return json.dumps({"error": f"Variable '{var_name}' not found in scope '{scope}'. "
                           f"Available scopes: {snap.all_scope_names()}"})
    structure = _infer_schema(var)
    return json.dumps({"name": var.name, "type": var.type_name, "structure": structure})


def _get_variable_value_impl(
    workspace_path: str,
    var_name: str,
    scope: str = "locals",
    path: str = "",
    start: int = 0,
    end: int = 50,
) -> str:
    """Get the value of a variable with type-aware chunked access."""
    snap = DebugSnapshot.load_latest(workspace_path)
    var = snap.find_var(var_name, scope)
    if var is None:
        return json.dumps({"error": f"Variable '{var_name}' not found in scope '{scope}'. "
                           f"Available scopes: {snap.all_scope_names()}"})
    return json.dumps(_format_value(var, path, start, end))


def _debug_eval_impl(workspace_path: str, expression: str, frame_id: int = 0) -> str:
    """Evaluate an arbitrary expression in the paused debug session's REPL."""
    debug_dir = Path(workspace_path) / ".waterfree" / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    request_id = str(uuid.uuid4())
    request_path = debug_dir / "eval_request.json"
    response_path = debug_dir / "eval_response.json"

    if response_path.exists():
        try:
            response_path.unlink()
        except OSError:
            pass

    payload: dict[str, Any] = {
        "requestId": request_id,
        "expression": expression,
        "timestamp": _now_iso(),
    }
    if frame_id:
        payload["frameId"] = frame_id
    request_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    deadline = time.monotonic() + _EVAL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_EVAL_POLL_INTERVAL)
        if not response_path.exists():
            continue
        try:
            resp_data = json.loads(response_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if resp_data.get("requestId") != request_id:
            continue

        try:
            response_path.unlink()
            request_path.unlink()
        except OSError:
            pass

        if "error" in resp_data:
            return json.dumps({"error": resp_data["error"], "expression": expression})
        return json.dumps({
            "result": resp_data.get("result", ""),
            "resultType": resp_data.get("resultType", ""),
            "expression": expression,
        })

    try:
        request_path.unlink()
    except OSError:
        pass
    return json.dumps({
        "error": f"Eval timed out after {_EVAL_TIMEOUT_SECONDS}s. "
                 "Ensure the debugger is paused at a breakpoint in VS Code.",
        "expression": expression,
    })


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _read_snippet(file: str, line: int, workspace_path: str, radius: int = 5) -> str:
    try:
        p = Path(file)
        if not p.is_absolute() and workspace_path:
            p = Path(workspace_path) / p
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, line - 1 - radius)
        end = min(len(lines), line + radius)
        parts = []
        for i, ln in enumerate(lines[start:end], start=start + 1):
            marker = ">>" if i == line else "  "
            parts.append(f"{marker} {i:4d}  {ln}")
        return "\n".join(parts)
    except OSError:
        return ""


def _infer_schema(var: VarInfo) -> dict:
    t = var.type_name.lower()
    raw = var.raw_value

    if any(kw in t for kw in ("list", "array", "tuple", "sequence", "deque")):
        return {"kind": "array", "length": _detect_length(raw), "elementType": "unknown"}

    if any(kw in t for kw in ("dict", "object", "map", "namespace", "ordereddic")):
        return {"kind": "object", "keys": _detect_keys(raw)}

    if any(kw in t for kw in ("dataframe", "ndarray", "series", "matrix", "tensor")):
        return {"kind": "table", "shape": _detect_shape(raw)}

    return {"kind": "primitive", "type": var.type_name, "preview": raw[:100]}


def _detect_length(raw: str) -> Optional[int]:
    m = re.search(r"\b(\d+)\s*(items?|elements?|entries|rows?)\b", raw, re.IGNORECASE)
    if m:
        return int(m.group(1))
    inner = re.match(r"^\[(.+)\]$", raw.strip(), re.DOTALL)
    if inner:
        return inner.group(1).count(",") + 1
    return None


def _detect_keys(raw: str) -> list[str]:
    keys = re.findall(r"['\"]([^'\"]+)['\"](?:\s*:)", raw[:2000])
    return keys[:20]


def _detect_shape(raw: str) -> str:
    m = re.search(r"\([\d,\s]+\)", raw)
    return m.group(0) if m else raw[:80]


def _format_value(var: VarInfo, path_str: str, start: int, end: int) -> dict:
    t = var.type_name.lower()
    raw = var.raw_value

    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass

    if path_str and parsed is not None:
        parsed = _navigate_path(parsed, path_str)
        if parsed is None:
            return {"error": f"Path '{path_str}' not found in variable '{var.name}'"}

    if any(kw in t for kw in ("list", "array", "tuple", "deque")) or isinstance(parsed, list):
        items = parsed if isinstance(parsed, list) else _parse_list_repr(raw)
        total = len(items) if items is not None else None
        sliced = items[start:end] if items is not None else None
        return {
            "type": "array",
            "total": total,
            "items": [str(i)[:200] for i in sliced] if sliced is not None else [raw[:_MAX_RESPONSE_CHARS]],
            "range": f"{start}-{min(end, total or end)} of {total}" if total is not None else None,
        }

    if any(kw in t for kw in ("dict", "object", "map", "namespace")) or isinstance(parsed, dict):
        d = parsed if isinstance(parsed, dict) else {}
        keys = list(d.keys())
        preview = {str(k): str(v)[:100] for k, v in list(d.items())[:10]}
        return {"type": "object", "keyCount": len(keys), "keys": keys[:50], "preview": preview}

    if any(kw in t for kw in ("dataframe", "ndarray", "series", "matrix", "tensor")):
        return {"type": "table", "rawPreview": raw[:_MAX_RESPONSE_CHARS]}

    return {"type": "primitive", "value": raw[:_MAX_RESPONSE_CHARS]}


def _navigate_path(obj: Any, path_str: str) -> Any:
    tokens: list[str] = []
    for part in re.split(r"\.", path_str):
        segments = re.split(r"\[(\d+)\]", part)
        for seg in segments:
            if seg:
                tokens.append(seg)
    for token in tokens:
        try:
            if isinstance(obj, dict):
                obj = obj[token]
            elif isinstance(obj, (list, tuple)) and token.isdigit():
                obj = obj[int(token)]
            else:
                return None
        except (KeyError, IndexError, TypeError):
            return None
    return obj


def _parse_list_repr(raw: str) -> Optional[list[str]]:
    inner = re.match(r"^\[(.+)\]$", raw.strip(), re.DOTALL)
    if not inner:
        return None
    return [s.strip() for s in inner.group(1).split(",")]
