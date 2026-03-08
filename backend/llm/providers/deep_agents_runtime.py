"""
Deep Agents runtime lane.

This adapter currently uses Anthropic execution as the default engine while
adding skill selection and subagent metadata hooks so the runtime contract is
fully available.
"""

from __future__ import annotations

from backend.llm.personas import DEFAULT_PERSONA, PERSONAS
from backend.llm.skills import SkillAdapter, SkillRegistry

from .anthropic_runtime import AnthropicRuntime


class DeepAgentsRuntime(AnthropicRuntime):
    def __init__(self, *args, workspace_path: str = ".", **kwargs):
        super().__init__(*args, **kwargs)
        self._workspace_path = workspace_path
        self._skill_registry = SkillRegistry(workspace_path)
        self._skill_adapter = SkillAdapter(self._skill_registry)

    @property
    def runtime_id(self) -> str:
        return "deep_agents"

    def generate_plan(
        self,
        goal: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ):
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage="planning")
        return super().generate_plan(
            goal,
            self._skill_adapter.augment_context(context, bundle),
            workspace_path=workspace_path,
            persona=persona,
        )

    def generate_annotation(
        self,
        task,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ):
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage="annotation")
        return super().generate_annotation(
            task,
            self._skill_adapter.augment_context(context, bundle),
            workspace_path=workspace_path,
            persona=persona,
        )

    def execute_task(
        self,
        task,
        context: str,
        workspace_path: str = "",
        on_chunk=None,
        persona: str = "default",
    ):
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(persona),
            stage="execution",
            task_type=getattr(task, "task_type", ""),
        )
        return super().execute_task(
            task,
            self._skill_adapter.augment_context(context, bundle),
            workspace_path=workspace_path,
            on_chunk=on_chunk,
            persona=persona,
        )

    def list_subagents(self) -> list[dict]:
        return [
            {"id": "architect", "label": "Architect", "skills": ["waterfree-index", "waterfree-knowledge"]},
            {"id": "pattern_expert", "label": "Pattern Expert", "skills": ["waterfree-knowledge"]},
            {"id": "debug_detective", "label": "Debug Detective", "skills": ["waterfree-debug", "waterfree-index"]},
            {"id": "stub_wireframer", "label": "Stub/Wireframes", "skills": ["waterfree-todos"]},
        ]

    def delegate_to_subagent(
        self,
        *,
        session_id: str,
        subagent_id: str,
        task_id: str,
        prompt: str,
        workspace_path: str = "",
    ) -> dict:
        checkpoint = self.checkpoint(
            session_id=session_id,
            reason="subagent_delegation",
            payload={
                "summary": f"Delegated task {task_id} to {subagent_id}",
                "subagentId": subagent_id,
                "prompt": prompt,
                "workspacePath": workspace_path,
                "taskId": task_id,
                "requiresApproval": True,
                "toolCalls": [],
                "touchedFiles": [],
            },
        )
        return {"checkpointId": checkpoint["id"], "result": None}

    def refresh_skills(self) -> int:
        return len(self._skill_registry.reload())

    def list_skills(self, persona: str = "", stage: str = "") -> list[dict]:
        return self._skill_registry.to_dicts(persona=persona, stage=stage)

    def get_skill_detail(self, skill_id: str) -> dict:
        return self._skill_registry.get_skill_detail(skill_id)


def _normalize_persona(persona: str) -> str:
    candidate = (persona or "").strip().lower()
    if candidate in PERSONAS:
        return candidate
    return DEFAULT_PERSONA
