"""
DebugSnapshot — reads .waterfree/debug/snapshot.json written by the
TypeScript extension after the user clicks "Push to Agent" in the sidebar.

The snapshot is the contract between the extension (writer) and mcp_debug.py
(reader). It captures breakpoint state with user-annotated intent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class VarInfo:
    name: str
    type_name: str
    raw_value: str
    variables_reference: int = 0


@dataclass
class CallFrame:
    frame: str
    file: str
    line: int


@dataclass
class Location:
    file: str
    line: int
    qualified_name: str


@dataclass
class DebugSnapshot:
    captured_at: str
    intent: str
    stop_reason: str
    location: Location
    call_stack: list[CallFrame] = field(default_factory=list)
    scopes: dict[str, list[VarInfo]] = field(default_factory=dict)
    exception_message: Optional[str] = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load_latest(cls, workspace_path: str) -> "DebugSnapshot":
        snapshot_path = Path(workspace_path) / ".waterfree" / "debug" / "snapshot.json"
        if not snapshot_path.exists():
            raise FileNotFoundError(
                f"No debug snapshot at {snapshot_path}. "
                "Open the PairProgram panel, fill in your intent, and click 'Push to Agent'."
            )

        data = json.loads(snapshot_path.read_text(encoding="utf-8"))

        loc_data = data.get("location", {})
        location = Location(
            file=loc_data.get("file", ""),
            line=loc_data.get("line", 0),
            qualified_name=loc_data.get("qualifiedName", ""),
        )

        call_stack = [
            CallFrame(
                frame=f.get("frame", ""),
                file=f.get("file", ""),
                line=f.get("line", 0),
            )
            for f in data.get("callStack", [])
        ]

        scopes: dict[str, list[VarInfo]] = {}
        for scope_name, vars_list in data.get("scopes", {}).items():
            scopes[scope_name] = [
                VarInfo(
                    name=v.get("name", ""),
                    type_name=v.get("type", ""),
                    raw_value=v.get("rawValue", ""),
                    variables_reference=v.get("variablesReference", 0),
                )
                for v in (vars_list if isinstance(vars_list, list) else [])
            ]

        return cls(
            captured_at=data.get("capturedAt", ""),
            intent=data.get("intent", ""),
            stop_reason=data.get("stopReason", ""),
            location=location,
            call_stack=call_stack,
            scopes=scopes,
            exception_message=data.get("exceptionMessage"),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_stale(self, max_age_seconds: int = 3600) -> bool:
        """Return True if the snapshot is older than max_age_seconds."""
        try:
            captured = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - captured).total_seconds()
            return age > max_age_seconds
        except (ValueError, TypeError):
            return True

    def find_var(self, var_name: str, scope: str = "") -> Optional[VarInfo]:
        """Find a variable by name, optionally restricted to a scope."""
        scopes_to_search = [scope] if scope and scope in self.scopes else list(self.scopes.keys())
        for s in scopes_to_search:
            for v in self.scopes.get(s, []):
                if v.name == var_name:
                    return v
        return None

    def all_scope_names(self) -> list[str]:
        return list(self.scopes.keys())
