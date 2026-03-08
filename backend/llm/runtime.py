"""
Runtime boundary for provider-specific LLM backends.

The backend should depend on this protocol rather than a concrete provider
client so Anthropic, Deep Agents, Ollama, or future runtimes can be swapped in.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from backend.session.models import IntentAnnotation, Task


class AgentRuntime(Protocol):
    def generate_plan(
        self,
        goal: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> tuple[list[Task], list[str]]:
        ...

    def generate_annotation(
        self,
        task: Task,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> IntentAnnotation:
        ...

    def execute_task(
        self,
        task: Task,
        context: str,
        workspace_path: str = "",
        on_chunk: Optional[Callable[[str], None]] = None,
        persona: str = "default",
    ) -> list[dict]:
        ...

    def detect_ripple(self, task: Task, scan_context: str, workspace_path: str = "") -> str:
        ...

    def alter_annotation(
        self,
        task: Task,
        old_annotation: IntentAnnotation,
        feedback: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> IntentAnnotation:
        ...

    def analyze_debug_context(
        self,
        debug_context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> dict:
        ...

    def answer_question(
        self,
        question: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> dict:
        ...

    def checkpoint(self, session_id: str, reason: str, payload: dict) -> dict:
        ...

    def resume(self, checkpoint_id: str, decision: dict) -> dict:
        ...
