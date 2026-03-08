"""
Checkpoint policy helpers for tool calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCallPolicyDecision:
    requires_checkpoint: bool
    requires_approval: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requiresCheckpoint": self.requires_checkpoint,
            "requiresApproval": self.requires_approval,
            "reason": self.reason,
        }


def evaluate_tool_call_policy(tool_metadata: dict[str, Any]) -> ToolCallPolicyDecision:
    requires_network = bool(tool_metadata.get("requiresNetwork", False))
    read_only = bool(tool_metadata.get("readOnly", True))
    explicit_approval = bool(tool_metadata.get("requiresApproval", False))

    if explicit_approval:
        return ToolCallPolicyDecision(
            requires_checkpoint=True,
            requires_approval=True,
            reason="Tool policy requires explicit approval.",
        )
    if requires_network:
        return ToolCallPolicyDecision(
            requires_checkpoint=True,
            requires_approval=True,
            reason="Networked tool call requires approval checkpoint.",
        )
    if not read_only:
        return ToolCallPolicyDecision(
            requires_checkpoint=True,
            requires_approval=False,
            reason="Write-capable tool call must be checkpointed.",
        )
    return ToolCallPolicyDecision(
        requires_checkpoint=False,
        requires_approval=False,
        reason="No checkpoint required by policy.",
    )
