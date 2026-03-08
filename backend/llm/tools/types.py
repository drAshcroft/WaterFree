"""
Shared tool descriptor types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any], str], dict[str, Any]]


@dataclass(frozen=True)
class ToolPolicy:
    read_only: bool = True
    requires_network: bool = False
    requires_approval: bool = False
    optional: bool = False
    category: str = "local"


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    server_id: str = "waterfree-core"
    title: str = ""

    def to_anthropic_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_policy_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title or self.name,
            "serverId": self.server_id,
            "readOnly": self.policy.read_only,
            "requiresNetwork": self.policy.requires_network,
            "requiresApproval": self.policy.requires_approval,
            "category": self.policy.category,
            "optional": self.policy.optional,
        }
