"""
Anthropic runtime adapter.

This keeps current Claude behavior but exposes it behind the shared runtime
boundary for future provider/runtime routing.
"""

from __future__ import annotations

from backend.llm.claude_client import ClaudeClient


class AnthropicRuntime(ClaudeClient):
    def checkpoint(self, session_id: str, reason: str, payload: dict) -> dict:
        raise NotImplementedError("Checkpointing is not implemented for the Anthropic runtime yet.")

    def resume(self, checkpoint_id: str, decision: dict) -> dict:
        raise NotImplementedError("Checkpoint resume is not implemented for the Anthropic runtime yet.")
