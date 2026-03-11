"""
Monitor LLM runtime — captures every prompt/context to a file and waits for
a human-supplied response before returning.

Intended for development and evaluation: instead of calling a real model the
runtime writes a capture file describing exactly what would be sent, then
polls for a matching response file that you (or a webview tool) write.

Capture directory (default: ``{workspace}/.waterfree/monitor/``):

  capture_{id}.json   — written by this runtime, contains full context
  response_{id}.json  — written by you (or the MonitorPanel webview)

Capture file schema:
    {
        "id":        "<8-char hex>",
        "stage":     "PLANNING",
        "persona":   "default",
        "system":    "...system prompt...",
        "user":      "...user prompt...",
        "timestamp": "2026-03-10T12:00:00Z",
        "status":    "pending"
    }

Response file schema:
    { "response": "...text or JSON string..." }

If no response appears within ``timeout`` seconds the runtime returns an
empty string / empty dict / empty collection (matching the protocol contract
for each method).

Environment variables:
    WATERFREE_MONITOR_DIR     — override the capture directory
    WATERFREE_MONITOR_TIMEOUT — override the timeout (seconds, float)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend.llm.prompt_templates import build_system_prompt
from backend.llm.personas import DEFAULT_PERSONA, PERSONAS
from backend.llm.structural_support import apply_task_dependencies, route_structural_persona, task_from_raw
from backend.llm.skills import SkillAdapter, SkillRegistry
from backend.llm.checkpoints.store import CheckpointStore
from backend.session.models import (
    AnnotationStatus,
    CodeCoord,
    IntentAnnotation,
    Task,
)
from backend.todo.store import TaskStore

_DEFAULT_TIMEOUT = 300.0
_POLL_INTERVAL = 0.5


def _normalize_persona(persona: str) -> str:
    candidate = (persona or "").strip().lower()
    return candidate if candidate in PERSONAS else DEFAULT_PERSONA


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    import re
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    for candidate in reversed(re.findall(r"\{.*\}", text, flags=re.DOTALL)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    return None


class MonitorRuntime:
    """
    AgentRuntime that captures prompts to disk and waits for human responses.

    The VS Code MonitorPanel (or any file-writing tool) provides the response
    by writing ``response_{id}.json`` in the same directory as the capture.
    """

    def __init__(
        self,
        *,
        workspace_path: str = ".",
        capture_dir: Optional[str] = None,
        timeout: Optional[float] = None,
        task_store_factory: Optional[Callable[[str], TaskStore]] = None,
        checkpoint_store_factory: Optional[Callable[[str], CheckpointStore]] = None,
    ) -> None:
        self._workspace_path = workspace_path
        self._capture_dir = (
            capture_dir
            or os.environ.get("WATERFREE_MONITOR_DIR", "")
            or os.path.join(workspace_path, ".waterfree", "monitor")
        )
        env_timeout = os.environ.get("WATERFREE_MONITOR_TIMEOUT", "")
        self._timeout = timeout if timeout is not None else (
            float(env_timeout) if env_timeout else _DEFAULT_TIMEOUT
        )

        self._task_store_factory = task_store_factory or (lambda wp: TaskStore(wp))
        self._checkpoint_store_factory = checkpoint_store_factory or (
            lambda wp: CheckpointStore(wp)
        )
        self._task_stores: dict[str, TaskStore] = {}
        self._checkpoint_stores: dict[str, CheckpointStore] = {}

        self._skill_registry = SkillRegistry(workspace_path)
        self._skill_adapter = SkillAdapter(self._skill_registry)

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def runtime_id(self) -> str:
        return "monitor"

    @property
    def capture_dir(self) -> str:
        return self._capture_dir

    # ------------------------------------------------------------------
    # Core capture / await
    # ------------------------------------------------------------------

    def _capture_and_await(
        self,
        *,
        stage: str,
        persona: str,
        system: str,
        user: str,
    ) -> str:
        """Write a capture file and block until a response file appears."""
        call_id = uuid.uuid4().hex[:12]
        os.makedirs(self._capture_dir, exist_ok=True)

        capture_path = os.path.join(self._capture_dir, f"capture_{call_id}.json")
        response_path = os.path.join(self._capture_dir, f"response_{call_id}.json")

        capture = {
            "id": call_id,
            "stage": stage,
            "persona": persona,
            "system": system,
            "user": user,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        with open(capture_path, "w", encoding="utf-8") as fh:
            json.dump(capture, fh, indent=2, ensure_ascii=False)

        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if os.path.exists(response_path):
                try:
                    with open(response_path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    return str(data.get("response", ""))
                except Exception:
                    pass
            time.sleep(_POLL_INTERVAL)

        return ""  # timeout — caller handles empty response gracefully

    def _capture_structured(
        self, *, stage: str, persona: str, system: str, user: str
    ) -> Optional[dict[str, Any]]:
        text = self._capture_and_await(stage=stage, persona=persona, system=system, user=user)
        return _extract_json_object(text) if text else None

    # ------------------------------------------------------------------
    # AgentRuntime protocol
    # ------------------------------------------------------------------

    def generate_plan(
        self,
        goal: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ):
        effective_persona = route_structural_persona(persona, "planning", goal, context)
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(effective_persona), stage="planning"
        )
        system = build_system_prompt("PLANNING", effective_persona)
        user = (
            "Return JSON only with shape: "
            '{"tasks":[{"id":"","title":"","description":"","rationale":"","targetFile":"","targetFunction":"","priority":"P0|P1|P2|P3|spike","phase":"","taskType":"impl|test|spike|review|refactor","contextCoords":[{"file":"","class":"","method":"","line":0,"anchorType":"modify"}],"dependsOn":[{"taskId":"","title":"","type":"blocks|informs|shares-file"}],"owner":{"type":"human|agent|unassigned","name":""},"estimatedMinutes":0,"aiNotes":""}],"designArtifacts":{"subsystems":[],"interfaces":[],"interfaceMethods":[],"dataContracts":[],"apiCatalog":[],"patternChoices":[],"antiPatterns":[],"integrationPolicies":[],"todos":[]},"questions":[]}\n\n'
            f"GOAL:\n{goal}\n\nCONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._capture_structured(
            stage="PLANNING", persona=effective_persona, system=system, user=user
        )
        if payload is None:
            return ([], [])
        raw_tasks = [t for t in payload.get("tasks", []) if isinstance(t, dict)]
        tasks = [task_from_raw(r) for r in raw_tasks]
        apply_task_dependencies(tasks, raw_tasks)
        return tasks, list(payload.get("questions", []))

    def generate_annotation(
        self,
        task: Task,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> IntentAnnotation:
        effective_persona = route_structural_persona(
            persona, "annotation", task.title, task.description, context
        )
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(effective_persona), stage="annotation"
        )
        system = build_system_prompt("ANNOTATION", effective_persona)
        user = (
            "Return JSON only with shape: "
            '{"summary":"","detail":"","willCreate":[],"willModify":[],"sideEffectWarnings":[],"assumptionsMade":[],"questionsBeforeProceeding":[]}\n\n'
            f"TASK TITLE: {task.title}\n"
            f"TASK DESCRIPTION: {task.description}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._capture_structured(
            stage="ANNOTATION", persona=effective_persona, system=system, user=user
        )
        if payload is None:
            return IntentAnnotation(
                task_id=task.id,
                target_coord=CodeCoord(
                    file=task.target_file, line=task.target_line, method=task.target_function
                ),
                summary="Monitor runtime: no response received within timeout.",
                detail="Write a response_{id}.json file in the capture directory.",
                status=AnnotationStatus.PENDING,
            )
        return IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(
                file=task.target_file, line=task.target_line, method=task.target_function
            ),
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
        task: Task,
        context: str,
        workspace_path: str = "",
        on_chunk: Optional[Callable[[str], None]] = None,
        persona: str = "default",
    ) -> list[dict]:
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(persona), stage="execution",
            task_type=getattr(task, "task_type", ""),
        )
        system = build_system_prompt("EXECUTION", persona)
        user = (
            "Return JSON only with shape: "
            '{"edits":[{"targetFile":"","oldContent":"","newContent":"","explanation":""}]}\n\n'
            f"TASK TITLE: {task.title}\n"
            f"TASK DESCRIPTION: {task.description}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._capture_structured(
            stage="EXECUTION", persona=persona, system=system, user=user
        )
        if payload is None:
            return []
        return list(payload.get("edits", []))

    def detect_ripple(self, task: Task, scan_context: str, workspace_path: str = "") -> str:
        system = build_system_prompt("RIPPLE_DETECTION", "reviewer")
        user = (
            'Return JSON only with shape: {"summary":"","followup":[]}\n\n'
            f"TASK TITLE: {task.title}\n\nSCAN:\n{scan_context}"
        )
        payload = self._capture_structured(
            stage="RIPPLE_DETECTION", persona="reviewer", system=system, user=user
        )
        if isinstance(payload, dict):
            summary = str(payload.get("summary", "")).strip()
            if summary:
                return summary
        text = self._capture_and_await(
            stage="RIPPLE_DETECTION", persona="reviewer", system=system, user=scan_context
        )
        return text.strip()

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
            persona, "alter_annotation",
            task.title, task.description, feedback,
            old_annotation.summary, old_annotation.detail, context,
        )
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(effective_persona), stage="annotation"
        )
        system = build_system_prompt("ALTER_ANNOTATION", effective_persona)
        user = (
            "Return JSON only with shape: "
            '{"summary":"","detail":"","approach":"","willCreate":[],"willModify":[],"willDelete":[],"sideEffectWarnings":[],"assumptionsMade":[],"questionsBeforeProceeding":[]}\n\n'
            f"TASK TITLE: {task.title}\n"
            f"TASK DESCRIPTION: {task.description}\n\n"
            f"PREVIOUS ANNOTATION SUMMARY: {old_annotation.summary}\n"
            f"PREVIOUS ANNOTATION DETAIL: {old_annotation.detail}\n"
            f"DEVELOPER FEEDBACK: {feedback}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._capture_structured(
            stage="ALTER_ANNOTATION", persona=effective_persona, system=system, user=user
        )
        if payload is None:
            return IntentAnnotation(
                task_id=task.id,
                target_coord=CodeCoord(
                    file=task.target_file, line=task.target_line, method=task.target_function
                ),
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
            target_coord=CodeCoord(
                file=task.target_file, line=task.target_line, method=task.target_function
            ),
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
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(persona), stage="live_debug"
        )
        system = build_system_prompt("LIVE_DEBUG", persona)
        user = (
            "Return JSON only with shape: "
            '{"diagnosis":"","likelyCause":"","suggestedFix":{"summary":"","detail":"","targetFile":"","targetLine":0,"willModify":[],"willCreate":[],"sideEffectWarnings":[]},"questions":[]}\n\n'
            f"DEBUG CONTEXT:\n{self._skill_adapter.augment_context(debug_context, bundle)}"
        )
        payload = self._capture_structured(
            stage="LIVE_DEBUG", persona=persona, system=system, user=user
        )
        if payload is None:
            return {
                "diagnosis": "Monitor runtime: no response received within timeout.",
                "likelyCause": "",
                "suggestedFix": {
                    "summary": "", "detail": "", "targetFile": "",
                    "targetLine": 0, "willModify": [], "willCreate": [],
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
            persona, "question_answer", question, context
        )
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(effective_persona), stage="question_answer"
        )
        system = build_system_prompt("QUESTION_ANSWER", effective_persona)
        user = (
            "Return JSON only with shape: "
            '{"answer":"","shouldUpdatePlan":false,"followupTasks":[],"designArtifacts":{"subsystems":[],"interfaces":[],"interfaceMethods":[],"dataContracts":[],"apiCatalog":[],"patternChoices":[],"antiPatterns":[],"integrationPolicies":[],"todos":[]},"questions":[]}\n\n'
            f"QUESTION: {question}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._capture_structured(
            stage="QUESTION_ANSWER", persona=effective_persona, system=system, user=user
        )
        if payload is None:
            text = self._capture_and_await(
                stage="QUESTION_ANSWER", persona=effective_persona, system=system, user=user
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
        system = build_system_prompt("KNOWLEDGE", "default")
        user = (
            'Return JSON only with shape: {"selected":[<indices>]}\n\n'
            f"SOURCE REPO: {source_repo}\nFOCUS: {focus}\n\nBATCH:\n"
            + json.dumps(batch, ensure_ascii=True)
        )
        payload = self._capture_structured(
            stage="KNOWLEDGE", persona="default", system=system, user=user
        )
        if payload and isinstance(payload.get("selected"), list):
            return [int(i) for i in payload["selected"] if isinstance(i, (int, float))]
        return list(range(len(batch)))

    def describe_knowledge_batch(
        self,
        *,
        source_repo: str,
        focus: str,
        batch: list[dict],
        workspace_path: str = "",
    ) -> list[dict]:
        system = build_system_prompt("KNOWLEDGE", "default")
        user = (
            'Return JSON only with shape: {"descriptions":[{"index":0,"summary":"","tags":[]}]}\n\n'
            f"SOURCE REPO: {source_repo}\nFOCUS: {focus}\n\nBATCH:\n"
            + json.dumps(batch, ensure_ascii=True)
        )
        payload = self._capture_structured(
            stage="KNOWLEDGE", persona="default", system=system, user=user
        )
        if payload and isinstance(payload.get("descriptions"), list):
            return list(payload["descriptions"])
        return []

    def summarize_procedure_knowledge(
        self,
        *,
        context: str,
        focus: str = "",
        workspace_path: str = "",
    ) -> dict:
        system = build_system_prompt("KNOWLEDGE", "default")
        user = (
            'Return JSON only with shape: {"summary":"","keyPatterns":[],"antiPatterns":[]}\n\n'
            f"FOCUS: {focus}\n\nCONTEXT:\n{context}"
        )
        payload = self._capture_structured(
            stage="KNOWLEDGE", persona="default", system=system, user=user
        )
        return payload or {}

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
        system = build_system_prompt("WIZARD", persona)
        revision_note = f"\n\nREVISION REQUEST: {revision_request}" if revision_request else ""
        user = (
            f"STAGE KIND: {stage_kind}\nSTAGE TITLE: {stage_title}\n"
            f"GOAL:\n{goal}\n\nCONTEXT:\n{context}"
            f"\n\nCHUNK SPECS:\n{json.dumps(chunk_specs, ensure_ascii=True)}"
            + revision_note
        )
        payload = self._capture_structured(
            stage="WIZARD", persona=persona, system=system, user=user
        )
        return payload or {}

    # ------------------------------------------------------------------
    # Checkpoint / resume (in-memory for monitor sessions)
    # ------------------------------------------------------------------

    def checkpoint(self, session_id: str, reason: str, payload: dict) -> dict:
        store = self._checkpoint_stores.setdefault(
            session_id,
            self._checkpoint_store_factory(self._workspace_path),
        )
        return store.create_checkpoint(
            session_id=session_id,
            reason=reason,
            runtime_id=self.runtime_id,
            payload=payload,
        )

    def resume(self, checkpoint_id: str, decision: dict) -> dict:
        for store in self._checkpoint_stores.values():
            cp = store.get_checkpoint(checkpoint_id)
            if cp is not None:
                return store.resume_checkpoint(checkpoint_id, decision)
        raise ValueError(f"Checkpoint not found: {checkpoint_id}")

    # ------------------------------------------------------------------
    # Skill helpers
    # ------------------------------------------------------------------

    def refresh_skills(self) -> int:
        return len(self._skill_registry.reload())

    def list_skills(self, persona: str = "", stage: str = "") -> list[dict]:
        return self._skill_registry.to_dicts(persona=persona, stage=stage)

    def get_skill_detail(self, skill_id: str) -> dict:
        return self._skill_registry.get_skill_detail(skill_id)
