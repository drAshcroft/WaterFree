"""
Code coordinate types — precise pointers into source files.
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CoordAnchorType(str, Enum):
    CREATE_AT = "create-at"
    MODIFY = "modify"
    DELETE = "delete"
    READ_ONLY_CONTEXT = "read-only-context"


@dataclass
class CodeCoord:
    """Precise pointer into source code. Symbol name takes priority over line
    so annotations stay anchored when lines shift due to edits above the target."""
    file: str = ""                          # relative workspace path
    class_name: Optional[str] = None        # class name (if applicable)
    method: Optional[str] = None            # method/function name
    line: Optional[int] = None              # hint only — symbol name used first
    anchor_type: CoordAnchorType = CoordAnchorType.MODIFY

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "class": self.class_name,
            "method": self.method,
            "line": self.line,
            "anchorType": self.anchor_type.value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CodeCoord:
        return cls(
            file=d.get("file", ""),
            class_name=d.get("class"),
            method=d.get("method"),
            line=d.get("line"),
            anchor_type=CoordAnchorType(d.get("anchorType", "modify")),
        )
