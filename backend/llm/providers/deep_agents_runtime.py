"""
Deep Agents runtime lane.

Uses `deepagents` as the orchestration layer.  Falls back to empty/no-op
results when deepagents is unavailable or returns malformed payloads.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Optional

from backend.graph.client import GraphClient
from backend.knowledge.store import KnowledgeStore
from backend.llm.prompt_templates import build_system_prompt
from backend.llm.personas import DEFAULT_PERSONA, PERSONAS
from backend.llm.checkpoints.store import CheckpointStore
from backend.llm.skills import SkillAdapter, SkillRegistry
from backend.llm.tools import build_default_tool_registry
from backend.session.models import AnnotationStatus, CodeCoord, IntentAnnotation, Task, TaskPriority
from backend.todo.store import TaskStore

class DeepAgentsRuntime:
    def __init__(
        self,
        *,
        workspace_path: str = ".",
        provider_lane: str = "deep_agents",
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
        self._skill_registry = SkillRegistry(workspace_path)
        self._skill_adapter = SkillAdapter(self._skill_registry)
        self._tool_registry = build_default_tool_registry(
            graph=self._graph,
            task_store_factory=self._task_store_factory,
            knowledge_store_factory=self._get_knowledge_store,
            enable_optional_web_tools=bool(os.environ.get("WATERFREE_ENABLE_WEB_TOOLS", "").strip()),
        )
        self._deepagents_factory: Optional[Callable[..., Any]] = None
        self._filesystem_backend_factory: Optional[Callable[..., Any]] = None
        self._structured_tool_cls: Optional[type] = None
        self._field_cls: Optional[type] = None
        self._create_model_fn: Optional[Callable[..., Any]] = None
        self._deepagents_import_error: Optional[str] = None
        self._load_deepagents()

    @property
    def runtime_id(self) -> str:
        return self._provider_lane

    def generate_plan(
        self,
        goal: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ):
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage="planning")
        prompt = (
            "Return JSON only with shape: "
            '{"tasks":[{"title":"","description":"","targetFile":"","targetFunction":"","priority":"P0|P1|P2|P3|spike"}],"questions":[]}\n\n'
            f"GOAL:\n{goal}\n\nCONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="PLANNING",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
        )
        if payload is None:
            return ([], [])
        tasks: list[Task] = []
        for raw in payload.get("tasks", []):
            tasks.append(
                Task(
                    title=str(raw.get("title", "")),
                    description=str(raw.get("description", "")),
                    target_coord=CodeCoord(
                        file=str(raw.get("targetFile", "")),
                        method=str(raw.get("targetFunction", "")) or None,
                    ),
                    priority=_coerce_priority(raw.get("priority", "P2")),
                )
            )
        return tasks, list(payload.get("questions", []))

    def generate_annotation(
        self,
        task,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ):
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage="annotation")
        prompt = (
            "Return JSON only with shape: "
            '{"summary":"","detail":"","willCreate":[],"willModify":[],"sideEffectWarnings":[],"assumptionsMade":[],"questionsBeforeProceeding":[]}\n\n'
            f"TASK TITLE: {task.title}\n"
            f"TASK DESCRIPTION: {task.description}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="ANNOTATION",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
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

    def execute_task(
        self,
        task,
        context: str,
        workspace_path: str = "",
        on_chunk=None,  # noqa: ARG002 — streaming not used by this runtime
        persona: str = "default",
    ):
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(persona),
            stage="execution",
            task_type=getattr(task, "task_type", ""),
        )
        prompt = (
            "Return JSON only with shape: "
            '{"edits":[{"targetFile":"","oldContent":"","newContent":"","explanation":""}]}\n\n'
            f"TASK TITLE: {task.title}\n"
            f"TASK DESCRIPTION: {task.description}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="EXECUTION",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
        )
        if payload is None:
            return []
        return list(payload.get("edits", []))

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
            stage="RIPPLE_DETECTION",
            prompt=prompt,
            workspace_path=workspace_path,
            persona="reviewer",
        )
        if isinstance(payload, dict):
            summary = str(payload.get("summary", "")).strip()
            if summary:
                return summary
        return self._run_deepagents_text(
            stage="RIPPLE_DETECTION",
            prompt=scan_context,
            workspace_path=workspace_path,
            persona="reviewer",
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
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage="annotation")
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
            stage="ALTER_ANNOTATION",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
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
            stage="LIVE_DEBUG",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
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
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage="question_answer")
        prompt = (
            "Return JSON only with shape: "
            '{"answer":"","shouldUpdatePlan":false,"followupTasks":[],"questions":[]}\n\n'
            f"QUESTION: {question}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._run_deepagents_structured(
            stage="QUESTION_ANSWER",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
        )
        if payload is None:
            text = self._run_deepagents_text(
                stage="QUESTION_ANSWER",
                prompt=prompt,
                workspace_path=workspace_path,
                persona=persona,
            ).strip()
            return {
                "answer": text,
                "shouldUpdatePlan": False,
                "followupTasks": [],
                "questions": [],
            }
        return {
            "answer": str(payload.get("answer", "")),
            "shouldUpdatePlan": bool(payload.get("shouldUpdatePlan", False)),
            "followupTasks": list(payload.get("followupTasks", [])),
            "questions": list(payload.get("questions", [])),
        }

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
        metadata = metadata or {}
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage="planning")
        web_tools = bool(metadata.get("webToolsEnabled"))
        prompt = (
            "Return JSON only with shape: "
            '{"stageSummary":"","chunks":[{"id":"","content":""}],"todos":[{"title":"","description":"","prompt":"","phase":"","priority":"P0|P1|P2|P3|spike","taskType":"impl|test|spike|review|refactor","targetFile":"","targetFunction":"","ownerType":"human|agent|unassigned","ownerName":""}],"subsystems":[],"externalResearchPrompt":"","questions":[]}\n\n'
            f"STAGE KIND: {stage_kind}\n"
            f"STAGE TITLE: {stage_title}\n"
            f"GOAL: {goal}\n"
            f"WEB TOOLS AVAILABLE: {'yes' if web_tools else 'no'}\n"
            f"REVISION REQUEST: {revision_request.strip() or '(none)'}\n"
            f"METADATA: {json.dumps(metadata, ensure_ascii=True)}\n"
            f"CHUNKS TO DRAFT: {json.dumps(chunk_specs, ensure_ascii=True)}\n\n"
            "Rules:\n"
            "- Draft only the requested chunk ids.\n"
            "- Preserve the stage intent and produce concise markdown-ready prose.\n"
            "- Emit todo items only when the stage naturally produces follow-up work.\n"
            "- For architect review, include a realistic `subsystems` list.\n"
            "- For market research without web tools, provide an `externalResearchPrompt`.\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        prompt_stage = "QUESTION_ANSWER" if stage_kind == "review" else "PLANNING"
        payload = self._run_deepagents_structured(
            stage=prompt_stage,
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
        )
        if payload is None:
            return self._fallback_wizard_stage(
                stage_kind=stage_kind,
                stage_title=stage_title,
                goal=goal,
                chunk_specs=chunk_specs,
                revision_request=revision_request,
                metadata=metadata,
            )
        return payload

    def list_subagents(self) -> list[dict]:
        return [
            {"id": "architect", "label": "Architect", "skills": ["waterfree-index", "waterfree-knowledge"]},
            {"id": "pattern_expert", "label": "Pattern Expert", "skills": ["waterfree-knowledge"]},
            {"id": "debug_detective", "label": "Debug Detective", "skills": ["waterfree-debug", "waterfree-index"]},
            {"id": "stub_wireframer", "label": "Stub/Wireframes", "skills": ["waterfree-todos"]},
            {"id": "market_researcher", "label": "Market Researcher", "skills": ["waterfree-index", "waterfree-knowledge"]},
            {"id": "bdd_test_designer", "label": "BDD Test Designer", "skills": ["waterfree-todos"]},
            {"id": "coding_agent", "label": "Coding Agent", "skills": ["waterfree-todos"]},
            {"id": "reviewer", "label": "Reviewer", "skills": ["waterfree-index", "waterfree-todos"]},
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

    def _get_knowledge_store(self) -> KnowledgeStore:
        if self._knowledge_store is None:
            raise RuntimeError("No KnowledgeStore configured for this runtime.")
        return self._knowledge_store

    def _load_deepagents(self) -> None:
        try:
            from deepagents import create_deep_agent
            from deepagents.backends import FilesystemBackend
            from langchain_core.tools import StructuredTool
            from pydantic import Field, create_model

            self._deepagents_factory = create_deep_agent
            self._filesystem_backend_factory = FilesystemBackend
            self._structured_tool_cls = StructuredTool
            self._field_cls = Field
            self._create_model_fn = create_model
            self._deepagents_import_error = None
        except Exception as exc:
            self._deepagents_factory = None
            self._filesystem_backend_factory = None
            self._structured_tool_cls = None
            self._field_cls = None
            self._create_model_fn = None
            self._deepagents_import_error = str(exc)

    def _run_deepagents_structured(
        self,
        *,
        stage: str,
        prompt: str,
        workspace_path: str,
        persona: str,
    ) -> Optional[dict[str, Any]]:
        response_text = self._run_deepagents_text(
            stage=stage,
            prompt=prompt,
            workspace_path=workspace_path,
            persona=persona,
        )
        if response_text:
            parsed = _extract_json_object(response_text)
            if isinstance(parsed, dict):
                return parsed
        return None

    def _run_deepagents_text(
        self,
        *,
        stage: str,
        prompt: str,
        workspace_path: str,
        persona: str,
    ) -> str:
        if not self._deepagents_factory:
            return ""
        agent = self._create_agent(stage=stage, workspace_path=workspace_path, persona=persona)
        if agent is None:
            return ""
        try:
            result = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
            return _extract_response_text(result)
        except Exception:
            return ""

    def _create_agent(self, *, stage: str, workspace_path: str, persona: str):
        if not self._deepagents_factory:
            return None
        system_prompt = build_system_prompt(stage.upper(), persona)
        bundle = self._skill_adapter.select(persona=_normalize_persona(persona), stage=stage.lower())
        system_prompt = self._skill_adapter.augment_system_prompt(system_prompt, bundle)
        tools = self._build_langchain_tools(workspace_path=workspace_path)
        model_name = _model_name_for_lane(self._provider_lane)
        kwargs: dict[str, Any] = {
            "model": model_name,
            "tools": tools,
            "system_prompt": system_prompt,
            "subagents": self._deepagents_subagents(),
            "interrupt_on": self._interrupt_config(),
        }
        if self._filesystem_backend_factory:
            kwargs["backend"] = self._filesystem_backend_factory(root_dir=workspace_path or self._workspace_path)
        return self._deepagents_factory(**kwargs)

    def _build_langchain_tools(self, *, workspace_path: str) -> list[Any]:
        if not self._structured_tool_cls or not self._create_model_fn or not self._field_cls:
            return []
        tools: list[Any] = []
        for descriptor in self._tool_registry.list_descriptors(include_optional=False):
            if descriptor.policy.optional:
                continue
            args_schema = _schema_to_pydantic_model(
                name=descriptor.name,
                schema=descriptor.input_schema,
                create_model_fn=self._create_model_fn,
                field_cls=self._field_cls,
            )

            def make_runner(name: str):
                def _runner(**kwargs) -> str:
                    result = self._tool_registry.invoke(name, kwargs, workspace_path)
                    return json.dumps(result, ensure_ascii=True)

                _runner.__name__ = name
                return _runner

            tools.append(
                self._structured_tool_cls.from_function(
                    func=make_runner(descriptor.name),
                    name=descriptor.name,
                    description=descriptor.description,
                    args_schema=args_schema,
                )
            )
        return tools

    def _fallback_wizard_stage(
        self,
        *,
        stage_kind: str,
        stage_title: str,
        goal: str,
        chunk_specs: list[dict],
        revision_request: str,
        metadata: dict,
    ) -> dict:
        chunks = []
        for spec in chunk_specs:
            note_text = str(spec.get("notes", "")).strip()
            body = [
                f"{stage_title} draft for {goal}.",
                "",
                f"Chunk: {spec.get('title', spec.get('id', 'chunk'))}.",
            ]
            if note_text:
                body.extend(["", "Current notes:", note_text])
            if revision_request.strip():
                body.extend(["", "Revision request:", revision_request.strip()])
            chunks.append({
                "id": str(spec.get("id", "")),
                "content": "\n".join(body).strip(),
            })

        todos: list[dict[str, str]] = []
        if stage_kind == "architect_review":
            todos.append({
                "title": "Turn architect output into subsystem work",
                "description": f"Convert the accepted architect review for '{goal}' into subsystem plans.",
                "phase": stage_title,
                "priority": "P1",
                "taskType": "spike",
                "targetFile": "",
                "targetFunction": "",
                "ownerType": "unassigned",
                "ownerName": "",
                "prompt": "Use the accepted architect chunks to define subsystem work.",
            })
        elif stage_kind == "wireframe_agents":
            todos.append({
                "title": f"Implement wireframe for {metadata.get('subsystemName') or stage_title}",
                "description": f"Convert the accepted wireframe into coding work for {metadata.get('subsystemName') or stage_title}.",
                "phase": stage_title,
                "priority": "P1",
                "taskType": "impl",
                "targetFile": "",
                "targetFunction": "",
                "ownerType": "unassigned",
                "ownerName": "",
                "prompt": "Implement the accepted micro-prompts.",
            })
        elif stage_kind == "bdd_ai_tests":
            todos.append({
                "title": "Write BDD coverage",
                "description": f"Translate the accepted BDD stage for '{goal}' into real tests.",
                "phase": stage_title,
                "priority": "P1",
                "taskType": "test",
                "targetFile": "",
                "targetFunction": "",
                "ownerType": "unassigned",
                "ownerName": "",
                "prompt": "Implement the accepted BDD scenarios as tests.",
            })
        elif stage_kind == "coding_agents":
            todos.append({
                "title": f"Build {goal}",
                "description": "Execute the accepted coding handoff.",
                "phase": stage_title,
                "priority": "P1",
                "taskType": "impl",
                "targetFile": "",
                "targetFunction": "",
                "ownerType": "unassigned",
                "ownerName": "",
                "prompt": "Build the accepted coding tasks in order.",
            })

        subsystems = []
        if stage_kind == "architect_review":
            subsystems = ["Core Application", "API Layer", "Data Layer"]

        external_prompt = ""
        if stage_kind == "market_research" and not metadata.get("webToolsEnabled"):
            external_prompt = (
                "Research this software idea on the live web and return a concise market memo.\n\n"
                f"Idea: {goal}\n\n"
                "Cover:\n"
                "- comparable products and niches\n"
                "- what feels differentiated or weak\n"
                "- likely target audiences\n"
                "- realistic MVP\n"
                "- pricing or monetization signals if visible\n"
                "- risks or reasons the idea may fail\n"
            )

        return {
            "stageSummary": f"{stage_title} drafted for {goal}.",
            "chunks": chunks,
            "todos": todos,
            "subsystems": subsystems,
            "externalResearchPrompt": external_prompt,
            "questions": [],
        }

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
                "description": "Subsystem shell/stub generation",
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
                "description": "Turns accepted prompts into execution-ready coding tasks",
                "system_prompt": build_system_prompt("PLANNING", "coding_agent"),
            },
            {
                "name": "reviewer",
                "description": "Collects issues, blockers, and follow-up work",
                "system_prompt": build_system_prompt("QUESTION_ANSWER", "reviewer"),
            },
        ]


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


def _model_name_for_lane(provider_lane: str) -> str:
    lane = provider_lane.strip().lower()
    if lane == "openai":
        return os.environ.get("WATERFREE_OPENAI_MODEL", "openai:o3-mini")
    if lane == "ollama":
        return os.environ.get("WATERFREE_OLLAMA_MODEL", "ollama:qwen2.5-coder:14b")
    if lane == "anthropic":
        return os.environ.get("WATERFREE_ANTHROPIC_MODEL", "anthropic:claude-sonnet-4-20250514")
    return os.environ.get("WATERFREE_DEEPAGENTS_MODEL", "anthropic:claude-sonnet-4-20250514")


def _extract_response_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        messages = result.get("messages", [])
        for message in reversed(messages):
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for part in reversed(content):
                    text = part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
                    if isinstance(text, str) and text.strip():
                        return text
        return json.dumps(result, ensure_ascii=True)
    return str(result)


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    matches = re.findall(r"\{.*\}", text, flags=re.DOTALL)
    for candidate in reversed(matches):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None


def _schema_to_pydantic_model(name: str, schema: dict, create_model_fn, field_cls):
    props = dict(schema.get("properties", {}))
    required = set(schema.get("required", []))
    fields = {}
    for key, prop in props.items():
        py_type = _json_schema_type(prop)
        if key in required:
            default = ...
        else:
            default = None
        description = str(prop.get("description", ""))
        fields[key] = (py_type, field_cls(default=default, description=description))
    if not fields:
        return create_model_fn(f"{name.title().replace('_', '')}Input")
    return create_model_fn(f"{name.title().replace('_', '')}Input", **fields)


def _json_schema_type(prop: dict) -> Any:
    prop_type = prop.get("type")
    if prop_type == "string":
        return str
    if prop_type == "integer":
        return int
    if prop_type == "number":
        return float
    if prop_type == "boolean":
        return bool
    if prop_type == "array":
        inner = prop.get("items", {})
        return list[_json_schema_type(inner)]  # type: ignore[index]
    if prop_type == "object":
        return dict[str, Any]
    if "enum" in prop:
        return str
    return Any
