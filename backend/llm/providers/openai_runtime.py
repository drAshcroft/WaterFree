"""
OpenAI provider lane.

The current adapter keeps runtime compatibility by inheriting the shared
runtime behavior while exposing provider identity for routing and UI.
"""

from __future__ import annotations

from .anthropic_runtime import AnthropicRuntime


class OpenAIRuntime(AnthropicRuntime):
    @property
    def runtime_id(self) -> str:
        return "openai"
