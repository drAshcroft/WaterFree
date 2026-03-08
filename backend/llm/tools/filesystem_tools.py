"""
Workspace-bounded filesystem tool descriptors.
"""

from __future__ import annotations

import glob
from pathlib import Path

from .types import ToolDescriptor, ToolPolicy


def _resolve_workspace_path(workspace_path: str, raw_path: str) -> Path:
    workspace_root = Path(workspace_path).resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"Path is outside workspace: {raw_path}") from exc
    return resolved


def filesystem_tool_descriptors() -> list[ToolDescriptor]:
    def read_workspace_file(args: dict, workspace_path: str) -> dict:
        path = _resolve_workspace_path(workspace_path, str(args.get("path", "")))
        return {"content": path.read_text(encoding="utf-8"), "path": str(path)}

    def list_workspace_paths(args: dict, workspace_path: str) -> dict:
        pattern = str(args.get("glob", "**/*")) or "**/*"
        limit = int(args.get("limit", 200))
        root = Path(workspace_path).resolve()
        matches: list[str] = []
        for raw in glob.iglob(str(root / pattern), recursive=True):
            p = Path(raw)
            if not p.is_file():
                continue
            try:
                relative = str(p.resolve().relative_to(root))
            except ValueError:
                continue
            matches.append(relative.replace("\\", "/"))
            if len(matches) >= limit:
                break
        return {"paths": matches}

    def apply_workspace_patch(args: dict, workspace_path: str) -> dict:
        edits = list(args.get("edits", []))
        touched_files: list[str] = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            path = _resolve_workspace_path(workspace_path, str(edit.get("path", "")))
            operation = str(edit.get("op", "write")).lower()
            if operation == "delete":
                if path.exists():
                    path.unlink()
                    touched_files.append(str(path))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            content = str(edit.get("content", ""))
            path.write_text(content, encoding="utf-8")
            touched_files.append(str(path))
        return {"ok": True, "touchedFiles": touched_files}

    return [
        ToolDescriptor(
            name="read_workspace_file",
            title="read workspace file",
            description="Read a UTF-8 file from the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=read_workspace_file,
            policy=ToolPolicy(read_only=True, category="filesystem", requires_approval=False),
            server_id="waterfree-filesystem",
        ),
        ToolDescriptor(
            name="list_workspace_paths",
            title="list workspace paths",
            description="List workspace files matching a glob pattern.",
            input_schema={
                "type": "object",
                "properties": {"glob": {"type": "string"}, "limit": {"type": "integer"}},
            },
            handler=list_workspace_paths,
            policy=ToolPolicy(read_only=True, category="filesystem", requires_approval=False),
            server_id="waterfree-filesystem",
        ),
        ToolDescriptor(
            name="apply_workspace_patch",
            title="apply workspace patch",
            description="Apply bounded workspace file edits (write/create/delete).",
            input_schema={
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "op": {"type": "string", "enum": ["write", "delete"]},
                                "content": {"type": "string"},
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["edits"],
            },
            handler=apply_workspace_patch,
            policy=ToolPolicy(
                read_only=False,
                category="filesystem",
                requires_approval=True,
            ),
            server_id="waterfree-filesystem",
        ),
    ]
