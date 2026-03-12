"""
Deep Agents runtime lane — thin facade.

Delegates agent loading and execution to TaskExecutor, plan generation to
PlanGenerator, and wizard stages to WizardStageRunner.

The proxy methods _run_deepagents_structured and _run_deepagents_text are kept
on the facade so that existing tests can patch them via patch.object(runtime, …).
All public business-logic methods call these proxies (not the executor directly),
ensuring patches intercept correctly.

Store cache helpers are kept here as shared infrastructure.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from backend.graph.client import GraphClient
from backend.knowledge.store import KnowledgeStore
from backend.llm.provider_profiles import (
    ProviderProfileDocument,
    default_provider_profile_document,
    normalize_provider_profile,
)
from backend.llm.prompt_templates import build_system_prompt
from backend.llm.personas import DEFAULT_PERSONA, PERSONAS
from backend.llm.structural_support import (
    apply_task_dependencies,
    route_structural_persona,
    task_from_raw,
)
from backend.llm.skills import SkillAdapter, SkillRegistry
from backend.llm.tools import build_default_tool_registry
from backend.llm.checkpoints.store import CheckpointStore
from backend.session.models import (
    AnnotationStatus,
    CodeCoord,
    IntentAnnotation,
    Task,
    TaskPriority,
)
from backend.todo.store import TaskStore

from backend.llm.channels.registry import ChannelRegistry
from backend.llm.providers.task_executor import TaskExecutor
from backend.llm.providers.plan_generator import PlanGenerator
from backend.llm.providers.wizard_stage_runner import WizardStageRunner


class DeepAgentsRuntime:
    def __init__(
        self,
        *,
        workspace_path: str = ".",
        provider_lane: str = "deep_agents",
        provider_profile_document: Optional[ProviderProfileDocument] = None,
        graph: Optional[GraphClient] = None,
        knowledge_store: Optional[KnowledgeStore] = None,
        task_store_factory: Optional[Callable[[str], TaskStore]] = None,
        checkpoint_store_factory: Optional[Callable[[str], CheckpointStore]] = None,
    ):
        self._checkpoint_store_factory = checkpoint_store_factory or (lambda workspace: CheckpointStore(workspace))
        self._checkpoint_stores: dict[str, CheckpointStore] = {}
        self._task_store_factory = task_store_factory or (lambda workspace_path: TaskStore(workspace_path))
        self._task_stores: dict[str, TaskStore] = {}
        self._graph = graph
        self._knowledge_store = knowledge_store
        self._workspace_path = workspace_path
        self._provider_lane = provider_lane
        self._provider_profiles = provider_profile_document or default_provider_profile_document(provider_lane)

        self._skill_registry = SkillRegistry(workspace_path)
        self._skill_adapter = SkillAdapter(self._skill_registry)
        self._tool_registry = build_default_tool_registry(
            graph=self._graph,
            task_store_factory=self._task_store_factory,
            knowledge_store_factory=self._get_knowledge_store,
            enable_optional_web_tools=bool(os.environ.get("WATERFREE_ENABLE_WEB_TOOLS", "").strip()),
        )

        self._channel_registry = ChannelRegistry(workspace_path)
        self._executor = TaskExecutor(
            workspace_path=workspace_path,
            provider_lane=provider_lane,
            provider_profile_document=self._provider_profiles,
            tool_registry=self._tool_registry,
            skill_adapter=self._skill_adapter,
            interrupt_config_fn=self._interrupt_config,
            subagents_fn=self._deepagents_subagents,
            channel_registry=self._channel_registry,
        )
        self._plan_generator = PlanGenerator(
            executor=self._executor,
            skill_adapter=self._skill_adapter,
        )
        self._wizard_runner = WizardStageRunner(
            executor=self._executor,
            skill_adapter=self._skill_adapter,
        )

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def runtime_id(self) -> str:
        return self._provider_lane

    # ------------------------------------------------------------------
    # Proxy methods — kept here so tests can patch runtime._run_deepagents_*
    # All business-logic methods below call self._run_deepagents_structured /
    # self._run_deepagents_text so that patch.object(runtime, …) intercepts.
    # ------------------------------------------------------------------

    def _run_deepagents_structured(
        self,
        *,
        stage: str,
        prompt: str,
        workspace_path: str,
        persona: str,
        session_key: str = "",
    ) -> Optional[dict[str, Any]]:
        return self._executor._run_deepagents_structured(
            stage=stage,
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
            session_key=session_key,
        )

    def _run_deepagents_text(
        self,
        *,
        stage: str,
        prompt: str,
        workspace_path: str,
        persona: str,
        session_key: str = "",
    ) -> str:
        return self._executor._run_deepagents_text(
            stage=stage,
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
            session_key=session_key,
        )

    # ------------------------------------------------------------------
    # Planning & annotation — logic lives in PlanGenerator but calls are
    # routed through self._run_deepagents_structured so patches work.
    # ------------------------------------------------------------------

    def generate_plan(
        self,
        goal: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ):
        effective_persona = route_structural_persona(persona, "planning", goal, context)
        bundle = self._skill_adapter.select(persona=_normalize_persona(effective_persona), stage="planning")
        prompt = (
            "Return JSON only with shape: "
            '{"tasks":[{"id":"","title":"","description":"","rationale":"","targetFile":"","targetFunction":"","priority":"P0|P1|P2|P3|spike","phase":"","taskType":"impl|test|spike|review|refactor","contextCoords":[{"file":"","class":"","method":"","line":0,"anchorType":"modify"}],"dependsOn":[{"taskId":"","title":"","type":"blocks|informs|shares-file"}],"owner":{"type":"human|agent|unassigned","name":""},"estimatedMinutes":0,"aiNotes":""}],"designArtifacts":{"subsystems":[],"interfaces":[],"interfaceMethods":[],"dataContracts":[],"apiCatalog":[],"patternChoices":[],"antiPatterns":[],"integrationPolicies":[],"todos":[]},"questions":[]}\n\n'
            f"GOAL:\n{goal}\n\nCONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="PLANNING", prompt=prompt, workspace_path=workspace_path, persona=effective_persona,
        )
        if payload is None:
            return ([], [])
        raw_tasks = [item for item in payload.get("tasks", []) if isinstance(item, dict)]
        tasks = [task_from_raw(raw) for raw in raw_tasks]
        apply_task_dependencies(tasks, raw_tasks)
        return tasks, list(payload.get("questions", []))

    def generate_annotation(
        self,
        task,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ):
        effective_persona = route_structural_persona(
            persona,
            "annotation",
            task.title,
            task.description,
            context,
        )
        bundle = self._skill_adapter.select(persona=_normalize_persona(effective_persona), stage="annotation")
        prompt = (
            "Return JSON only with shape: "
            '{"summary":"","detail":"","willCreate":[],"willModify":[],"sideEffectWarnings":[],"assumptionsMade":[],"questionsBeforeProceeding":[]}\n\n'
            f"TASK TITLE: {task.title}\n"
            f"TASK DESCRIPTION: {task.description}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="ANNOTATION", prompt=prompt, workspace_path=workspace_path, persona=effective_persona,
        )
        if payload is None:
            return IntentAnnotation(
                task_id=task.id,
                target_coord=CodeCoord(file=task.target_file, line=task.target_line, method=task.target_function),
                summary="Deep Agents could not produce structured annotation output.",
                detail="Deep Agents unavailable or returned malformed output.",
                status=AnnotationStatus.PENDING,
            )
        return IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(file=task.target_file, line=task.target_line, method=task.target_function),
            summary=str(payload.get("summary", "")),
            detail=str(payload.get("detail", "")),
            will_create=list(payload.get("willCreate", [])),
            will_modify=list(payload.get("willModify", [])),
            side_effect_warnings=list(payload.get("sideEffectWarnings", [])),
            assumptions_made=list(payload.get("assumptionsMade", [])),
            questions_before_proceeding=list(payload.get("questionsBeforeProceeding", [])),
            status=AnnotationStatus.PENDING,
        )

    def detect_ripple(
        self,
        task: Task,
        scan_context: str,
        workspace_path: str = "",
    ) -> str:
        prompt = (
            'Return JSON only with shape: {"summary":"","followup":[]}\n\n'
            f"TASK TITLE: {task.title}\n\n"
            f"SCAN:\n{scan_context}"
        )
        payload = self._run_deepagents_structured(
            stage="RIPPLE_DETECTION", prompt=prompt, workspace_path=workspace_path, persona="reviewer",
        )
        if isinstance(payload, dict):
            summary = str(payload.get("summary", "")).strip()
            if summary:
                return summary
        return self._run_deepagents_text(
            stage="RIPPLE_DETECTION", prompt=scan_context, workspace_path=workspace_path, persona="reviewer",
        ).strip()

    def alter_annotation(
        self,
        task: Task,
        old_annotation: IntentAnnotation,
        feedback: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> IntentAnnotation:
        effective_persona = route_structural_persona(
            persona,
            "alter_annotation",
            task.title,
            task.description,
            feedback,
            old_annotation.summary,
            old_annotation.detail,
            context,
        )
        bundle = self._skill_adapter.select(persona=_normalize_persona(effective_persona), stage="annotation")
        prompt = (
            "Return JSON only with shape: "
            '{"summary":"","detail":"","approach":"","willCreate":[],"willModify":[],"willDelete":[],"sideEffectWarnings":[],"assumptionsMade":[],"questionsBeforeProceeding":[]}\n\n'
            f"TASK TITLE: {task.title}\n"
            f"TASK DESCRIPTION: {task.description}\n\n"
            f"PREVIOUS ANNOTATION SUMMARY: {old_annotation.summary}\n"
            f"PREVIOUS ANNOTATION DETAIL: {old_annotation.detail}\n"
            f"DEVELOPER FEEDBACK: {feedback}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="ALTER_ANNOTATION", prompt=prompt, workspace_path=workspace_path, persona=effective_persona,
        )
        if payload is None:
            return IntentAnnotation(
                task_id=task.id,
                target_coord=CodeCoord(file=task.target_file, line=task.target_line, method=task.target_function),
                summary=old_annotation.summary or "Annotation revised.",
                detail=(old_annotation.detail or "").strip() + f"\n\nDeveloper feedback: {feedback}",
                approach=old_annotation.approach,
                will_create=list(old_annotation.will_create),
                will_modify=list(old_annotation.will_modify),
                will_delete=list(old_annotation.will_delete),
                side_effect_warnings=list(old_annotation.side_effect_warnings),
                assumptions_made=list(old_annotation.assumptions_made),
                questions_before_proceeding=list(old_annotation.questions_before_proceeding),
                status=AnnotationStatus.PENDING,
            )
        return IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(file=task.target_file, line=task.target_line, method=task.target_function),
            summary=str(payload.get("summary", "")),
            detail=str(payload.get("detail", "")),
            approach=str(payload.get("approach", "")),
            will_create=list(payload.get("willCreate", [])),
            will_modify=list(payload.get("willModify", [])),
            will_delete=list(payload.get("willDelete", [])),
            side_effect_warnings=list(payload.get("sideEffectWarnings", [])),
            assumptions_made=list(payload.get("assumptionsMade", [])),
            questions_before_proceeding=list(payload.get("questionsBeforeProceeding", [])),
            status=AnnotationStatus.PENDING,
        )

    def analyze_debug_context(
        self,
        debug_context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> dict:
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage="live_debug")
        prompt = (
            "Return JSON only with shape: "
            '{"diagnosis":"","likelyCause":"","suggestedFix":{"summary":"","detail":"","targetFile":"","targetLine":0,"willModify":[],"willCreate":[],"sideEffectWarnings":[]},"questions":[]}\n\n'
            f"DEBUG CONTEXT:\n{self._skill_adapter.augment_context(debug_context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="LIVE_DEBUG", prompt=prompt, workspace_path=workspace_path, persona=persona,
        )
        if payload is None:
            return {
                "diagnosis": "Deep Agents could not analyze the debug context.",
                "likelyCause": "",
                "suggestedFix": {
                    "summary": "Review the breakpoint state manually.",
                    "detail": "No structured debug analysis was returned by the runtime.",
                    "targetFile": "",
                    "targetLine": 0,
                    "willModify": [],
                    "willCreate": [],
                    "sideEffectWarnings": [],
                },
                "questions": [],
            }
        suggested_fix = dict(payload.get("suggestedFix", {}))
        return {
            "diagnosis": str(payload.get("diagnosis", "")),
            "likelyCause": str(payload.get("likelyCause", "")),
            "suggestedFix": {
                "summary": str(suggested_fix.get("summary", "")),
                "detail": str(suggested_fix.get("detail", "")),
                "targetFile": str(suggested_fix.get("targetFile", "")),
                "targetLine": int(suggested_fix.get("targetLine", 0) or 0),
                "willModify": list(suggested_fix.get("willModify", [])),
                "willCreate": list(suggested_fix.get("willCreate", [])),
                "sideEffectWarnings": list(suggested_fix.get("sideEffectWarnings", [])),
            },
            "questions": list(payload.get("questions", [])),
        }

    def answer_question(
        self,
        question: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> dict:
        effective_persona = route_structural_persona(
            persona,
            "question_answer",
            question,
            context,
        )
        bundle = self._skill_adapter.select(persona=_normalize_persona(effective_persona), stage="question_answer")
        prompt = (
            "Return JSON only with shape: "
            '{"answer":"","shouldUpdatePlan":false,"followupTasks":[],"designArtifacts":{"subsystems":[],"interfaces":[],"interfaceMethods":[],"dataContracts":[],"apiCatalog":[],"patternChoices":[],"antiPatterns":[],"integrationPolicies":[],"todos":[]},"questions":[]}\n\n'
            f"QUESTION: {question}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="QUESTION_ANSWER", prompt=prompt, workspace_path=workspace_path, persona=effective_persona,
        )
        if payload is None:
            text = self._run_deepagents_text(
                stage="QUESTION_ANSWER", prompt=prompt, workspace_path=workspace_path, persona=effective_persona,
            ).strip()
            return {"answer": text, "shouldUpdatePlan": False, "followupTasks": [], "questions": []}
        return {
            "answer": str(payload.get("answer", "")),
            "shouldUpdatePlan": bool(payload.get("shouldUpdatePlan", False)),
            "followupTasks": list(payload.get("followupTasks", [])),
            "designArtifacts": dict(payload.get("designArtifacts", {})),
            "questions": list(payload.get("questions", [])),
        }

    def triage_knowledge_symbols(
        self,
        *,
        source_repo: str,
        focus: str,
        batch: list[dict],
        workspace_path: str = "",
    ) -> list[int]:
        return self._plan_generator.triage_knowledge_symbols(
            source_repo=source_repo, focus=focus, batch=batch, workspace_path=workspace_path,
        )

    def describe_knowledge_batch(
        self,
        *,
        source_repo: str,
        focus: str,
        batch: list[dict],
        workspace_path: str = "",
    ) -> list[dict]:
        return self._plan_generator.describe_knowledge_batch(
            source_repo=source_repo, focus=focus, batch=batch, workspace_path=workspace_path,
        )

    def summarize_procedure_knowledge(
        self,
        *,
        context: str,
        focus: str = "",
        workspace_path: str = "",
    ) -> dict:
        return self._plan_generator.summarize_procedure_knowledge(
            context=context, focus=focus, workspace_path=workspace_path,
        )

    # ------------------------------------------------------------------
    # Task execution — delegate to TaskExecutor
    # ------------------------------------------------------------------

    def execute_task(
        self,
        task,
        context: str,
        workspace_path: str = "",
        on_chunk=None,
        persona: str = "default",
    ):
        return self._executor.execute_task(task, context, workspace_path, on_chunk, persona)

    # ------------------------------------------------------------------
    # Wizard stage — delegate to WizardStageRunner
    # ------------------------------------------------------------------

    def run_wizard_stage(
        self,
        *,
        stage_kind: str,
        stage_title: str,
        goal: str,
        context: str,
        chunk_specs: list[dict],
        workspace_path: str = "",
        persona: str = "default",
        revision_request: str = "",
        metadata: Optional[dict] = None,
    ) -> dict:
        return self._wizard_runner.run_wizard_stage(
            stage_kind=stage_kind,
            stage_title=stage_title,
            goal=goal,
            context=context,
            chunk_specs=chunk_specs,
            workspace_path=workspace_path,
            persona=persona,
            revision_request=revision_request,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Subagent management
    # ------------------------------------------------------------------

    def list_subagents(self) -> list[dict]:
        return [
            {"id": "architect", "label": "Architect", "skills": ["waterfree-index", "waterfree-knowledge"]},
            {"id": "pattern_expert", "label": "Pattern Expert", "skills": ["waterfree-index", "waterfree-knowledge", "waterfree-todos"]},
            {"id": "debug_detective", "label": "Debug Detective", "skills": ["waterfree-debug", "waterfree-index"]},
            {
                "id": "stub_wireframer",
                "label": "Stub/Wireframes",
                "skills": ["waterfree-index", "waterfree-todos", "waterfree-testing"],
            },
            {"id": "market_researcher", "label": "Market Researcher", "skills": ["waterfree-index", "waterfree-knowledge"]},
            {"id": "bdd_test_designer", "label": "BDD Test Designer", "skills": ["waterfree-todos", "waterfree-testing"]},
            {
                "id": "coding_agent",
                "label": "Coding Agent",
                "skills": ["waterfree-index", "waterfree-knowledge", "waterfree-todos", "waterfree-testing"],
            },
            {"id": "reviewer", "label": "Reviewer", "skills": ["waterfree-index", "waterfree-todos", "waterfree-testing"]},
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
        cp = self.checkpoint(
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
        return {"checkpointId": cp["id"], "result": None}

    # ------------------------------------------------------------------
    # Usage stats
    # ------------------------------------------------------------------

    def get_usage_stats(self, workspace_path: str = "") -> list[dict]:
        """Return cumulative token usage per provider for this workspace."""
        return self._channel_registry.get_usage_stats()

    def flush_session(self, session_key: str) -> None:
        self._executor.flush_session(session_key)

    # ------------------------------------------------------------------
    # Skill management
    # ------------------------------------------------------------------

    def refresh_skills(self) -> int:
        return len(self._skill_registry.reload())

    def list_skills(self, persona: str = "", stage: str = "") -> list[dict]:
        return self._skill_registry.to_dicts(persona=persona, stage=stage)

    def get_skill_detail(self, skill_id: str) -> dict:
        return self._skill_registry.get_skill_detail(skill_id)

    # ------------------------------------------------------------------
    # Checkpoint / resume
    # ------------------------------------------------------------------

    def checkpoint(self, session_id: str, reason: str, payload: dict) -> dict:
        store = self._checkpoint_stores.setdefault(
            session_id,
            self._checkpoint_store_factory(self._workspace_path),
        )
        return store.create_checkpoint(
            session_id=session_id,
            reason=reason,
            runtime_id=self._provider_lane,
            payload=payload,
        )

    def resume(self, checkpoint_id: str, decision: dict) -> dict:
        for store in self._checkpoint_stores.values():
            cp = store.get_checkpoint(checkpoint_id)
            if cp is not None:
                return store.resume_checkpoint(checkpoint_id, decision)
        raise ValueError(f"Checkpoint not found: {checkpoint_id}")

    # ------------------------------------------------------------------
    # Shared infrastructure helpers
    # ------------------------------------------------------------------

    def _get_knowledge_store(self) -> KnowledgeStore:
        if self._knowledge_store is None:
            raise RuntimeError("No KnowledgeStore configured for this runtime.")
        return self._knowledge_store

    def _get_checkpoint_store(self, session_id: str) -> CheckpointStore:
        return self._checkpoint_stores.setdefault(
            session_id,
            self._checkpoint_store_factory(self._workspace_path),
        )

    def _get_task_store(self, workspace_path: str) -> TaskStore:
        return self._task_stores.setdefault(
            workspace_path,
            self._task_store_factory(workspace_path),
        )

    def _interrupt_config(self) -> dict[str, dict[str, list[str]]]:
        config: dict[str, dict[str, list[str]]] = {}
        for meta in self._tool_registry.policy_inventory(include_optional=False):
            if meta.get("requiresApproval"):
                config[str(meta["name"])] = {
                    "allowed_decisions": ["approve", "edit", "reject"],
                }
        return config

    def _deepagents_subagents(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "architect",
                "description": "Architecture synthesis and trade-off analysis",
                "system_prompt": build_system_prompt("PLANNING", "architect"),
            },
            {
                "name": "pattern_expert",
                "description": "Pattern and framework guidance",
                "system_prompt": build_system_prompt("PLANNING", "pattern_expert"),
            },
            {
                "name": "debug_detective",
                "description": "Root-cause focused debug analysis",
                "system_prompt": build_system_prompt("LIVE_DEBUG", "debug_detective"),
            },
            {
                "name": "stub_wireframer",
                "description": "Subsystem shell generation with TODO handoff and verification",
                "system_prompt": build_system_prompt("EXECUTION", "stub_wireframer"),
            },
            {
                "name": "market_researcher",
                "description": "Product framing, differentiation, and audience analysis",
                "system_prompt": build_system_prompt("PLANNING", "market_researcher"),
            },
            {
                "name": "bdd_test_designer",
                "description": "Acceptance scenarios and human-language test design",
                "system_prompt": build_system_prompt("PLANNING", "bdd_test_designer"),
            },
            {
                "name": "coding_agent",
                "description": "Implements code, escalates bad guidance, and drives execution-ready backlog work",
                "system_prompt": build_system_prompt("PLANNING", "coding_agent"),
            },
            {
                "name": "reviewer",
                "description": "Collects issues, blockers, and follow-up work",
                "system_prompt": build_system_prompt("QUESTION_ANSWER", "reviewer"),
            },
        ]


# ---------------------------------------------------------------------------
# Module-level helpers (re-exported from task_executor for backward compat)
# ---------------------------------------------------------------------------

def _normalize_persona(persona: str) -> str:
    candidate = (persona or "").strip().lower()
    if candidate in PERSONAS:
        return candidate
    return DEFAULT_PERSONA


def _coerce_priority(raw_priority: Any) -> TaskPriority:
    if isinstance(raw_priority, str):
        try:
            return TaskPriority(raw_priority)
        except ValueError:
            return TaskPriority.P2
    if isinstance(raw_priority, int):
        return {
            0: TaskPriority.P0,
            1: TaskPriority.P1,
            2: TaskPriority.P2,
            3: TaskPriority.P3,
        }.get(raw_priority, TaskPriority.P2)
    return TaskPriority.P2
