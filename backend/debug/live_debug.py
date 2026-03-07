"""
Live Pair Debug — DebugContext model and formatting for LLM analysis.

DebugContext is sent by the TypeScript extension after querying the
VS Code Debug Adapter Protocol. This module formats it for the LLM
and returns a structured DebugAnalysis.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_MAX_VARS = 100
_MAX_VALUE_LEN = 500
_SNIPPET_RADIUS = 10  # lines above/below breakpoint


@dataclass
class StackFrame:
    name: str
    file: str
    line: int
    column: int = 0


@dataclass
class DebugContext:
    file: str
    line: int                                            # 1-based
    stack_frames: list[StackFrame] = field(default_factory=list)
    variables: dict[str, dict[str, str]] = field(default_factory=dict)  # scope -> {name: value}
    exception_message: Optional[str] = None
    workspace_path: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> DebugContext:
        frames = [
            StackFrame(
                name=f.get("name", ""),
                file=f.get("file", ""),
                line=f.get("line", 0),
                column=f.get("column", 0),
            )
            for f in d.get("stackFrames", [])
        ]
        # Sanitise variable values
        variables: dict[str, dict[str, str]] = {}
        for scope, vars_dict in d.get("variables", {}).items():
            variables[scope] = {
                k: str(v)[:_MAX_VALUE_LEN]
                for k, v in list(vars_dict.items())[:_MAX_VARS]
            }
        return cls(
            file=d.get("file", ""),
            line=d.get("line", 0),
            stack_frames=frames,
            variables=variables,
            exception_message=d.get("exceptionMessage"),
            workspace_path=d.get("workspacePath", ""),
        )

    def format_for_llm(self) -> str:
        """Build the full debug context string to send to the LLM."""
        parts: list[str] = []

        # --- Code snippet ---
        code_snippet = self._read_snippet()
        if code_snippet:
            parts.append(f"CODE AT BREAKPOINT ({self.file}:{self.line}):\n{code_snippet}")

        # --- Exception ---
        if self.exception_message:
            parts.append(f"ACTIVE EXCEPTION:\n{self.exception_message}")

        # --- Call stack ---
        if self.stack_frames:
            stack_str = "\n".join(
                f"  {f.name}  ({f.file}:{f.line})" for f in self.stack_frames
            )
            parts.append(f"CALL STACK:\n{stack_str}")

        # --- Variables ---
        for scope, vars_dict in self.variables.items():
            if not vars_dict:
                continue
            var_lines = "\n".join(f"  {k} = {v}" for k, v in vars_dict.items())
            parts.append(f"VARIABLES [{scope}]:\n{var_lines}")

        if not parts:
            return f"(No debug data available for {self.file}:{self.line})"

        return "\n\n".join(parts)

    def _read_snippet(self) -> str:
        try:
            path = Path(self.file)
            if not path.is_absolute() and self.workspace_path:
                path = Path(self.workspace_path) / path
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, self.line - 1 - _SNIPPET_RADIUS)
            end = min(len(lines), self.line + _SNIPPET_RADIUS)
            snippet_lines = []
            for i, ln in enumerate(lines[start:end], start=start + 1):
                marker = ">>" if i == self.line else "  "
                snippet_lines.append(f"{marker} {i:4d}  {ln}")
            return "\n".join(snippet_lines)
        except OSError:
            return ""
