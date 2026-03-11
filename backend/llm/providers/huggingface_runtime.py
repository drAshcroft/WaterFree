"""
HuggingFace Inference API runtime.

Connects to HuggingFace serverless inference or a dedicated endpoint using
the `huggingface_hub` client. Implements the AgentRuntime protocol via
chat-completion calls with JSON-mode prompting (no tool loop).

Environment variables:
  WATERFREE_HF_TOKEN      - HuggingFace API token (required)
  WATERFREE_HF_MODEL      - Model repo id or alias
                            (default: meta-llama/Llama-3.1-8B-Instruct)
  WATERFREE_HF_BASE_URL   - Custom inference endpoint URL (optional)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Optional

from backend.llm.prompt_templates import build_system_prompt
from backend.llm.personas import DEFAULT_PERSONA, PERSONAS
from backend.llm.structural_support import (
    apply_task_dependencies,
    route_structural_persona,
    task_from_raw,
)
from backend.llm.skills import SkillAdapter, SkillRegistry
from backend.llm.checkpoints.store import CheckpointStore
from backend.session.models import (
    AnnotationStatus,
    CodeCoord,
    IntentAnnotation,
    Task,
)
from backend.todo.store import TaskStore

_DEFAULT_MODEL = "meta-llama/Llama-3.1-8B-Instruct"


def _env_model() -> str:
    return os.environ.get("WATERFREE_HF_MODEL", _DEFAULT_MODEL).strip()


def _env_token() -> str:
    return os.environ.get("WATERFREE_HF_TOKEN", "").strip()


def _env_base_url() -> Optional[str]:
    url = os.environ.get("WATERFREE_HF_BASE_URL", "").strip()
    return url or None


def _normalize_persona(persona: str) -> str:
    candidate = (persona or "").strip().lower()
    return candidate if candidate in PERSONAS else DEFAULT_PERSONA


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


class HuggingFaceRuntime:
    """AgentRuntime backed by the HuggingFace Inference API (chat completion)."""

    def __init__(
        self,
        *,
        workspace_path: str = ".",
        model: Optional[str] = None,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        task_store_factory: Optional[Callable[[str], TaskStore]] = None,
        checkpoint_store_factory: Optional[Callable[[str], CheckpointStore]] = None,
    ) -> None:
        self._workspace_path = workspace_path
        self._model = model or _env_model()
        self._token = token or _env_token()
        self._base_url = base_url or _env_base_url()

        self._task_store_factory = task_store_factory or (lambda wp: TaskStore(wp))
        self._checkpoint_store_factory = checkpoint_store_factory or (
            lambda wp: CheckpointStore(wp)
        )
        self._task_stores: dict[str, TaskStore] = {}
        self._checkpoint_stores: dict[str, CheckpointStore] = {}

        self._skill_registry = SkillRegistry(workspace_path)
        self._skill_adapter = SkillAdapter(self._skill_registry)

        self._client: Any = None
        self._import_error: Optional[str] = None
        self._load_client()

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def runtime_id(self) -> str:
        return "huggingface"

    # ------------------------------------------------------------------
    # Client initialisation
    # ------------------------------------------------------------------

    def _load_client(self) -> None:
        try:
            from huggingface_hub import InferenceClient  # type: ignore[import]

            kwargs: dict[str, Any] = {"token": self._token or None}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = InferenceClient(**kwargs)
        except Exception as exc:
            self._client = None
            self._import_error = str(exc)

    # ------------------------------------------------------------------
    # Core chat helpers
    # ------------------------------------------------------------------

    def _chat(self, *, system: str, user: str, max_tokens: int = 4096) -> str:
        """Single-turn chat completion. Returns the assistant message text."""
        if self._client is None:
            return ""
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
            )
            choice = response.choices[0]
            return (choice.message.content or "").strip()
        except Exception:
            return ""

    def _chat_structured(self, *, system: str, user: str) -> Optional[dict[str, Any]]:
        text = self._chat(system=system, user=user)
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
        payload = self._chat_structured(system=system, user=user)
        if payload is None:
            return ([], [])
        raw_tasks = [item for item in payload.get("tasks", []) if isinstance(item, dict)]
        tasks = [task_from_raw(raw) for raw in raw_tasks]
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
        payload = self._chat_structured(system=system, user=user)
        if payload is None:
            return IntentAnnotation(
                task_id=task.id,
                target_coord=CodeCoord(file=task.target_file, line=task.target_line, method=task.target_function),
                summary="HuggingFace runtime could not produce structured annotation output.",
                detail="Check WATERFREE_HF_TOKEN and WATERFREE_HF_MODEL.",
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
        task: Task,
        context: str,
        workspace_path: str = "",
        on_chunk: Optional[Callable[[str], None]] = None,
        persona: str = "default",
    ) -> list[dict]:
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(persona),
            stage="execution",
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
        payload = self._chat_structured(system=system, user=user)
        if payload is None:
            return []
        return list(payload.get("edits", []))

    def detect_ripple(self, task: Task, scan_context: str, workspace_path: str = "") -> str:
        system = build_system_prompt("RIPPLE_DETECTION", "reviewer")
        user = (
            'Return JSON only with shape: {"summary":"","followup":[]}\n\n'
            f"TASK TITLE: {task.title}\n\n"
            f"SCAN:\n{scan_context}"
        )
        payload = self._chat_structured(system=system, user=user)
        if isinstance(payload, dict):
            summary = str(payload.get("summary", "")).strip()
            if summary:
                return summary
        return self._chat(system=system, user=scan_context).strip()

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
        payload = self._chat_structured(system=system, user=user)
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
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(persona), stage="live_debug"
        )
        system = build_system_prompt("LIVE_DEBUG", persona)
        user = (
            "Return JSON only with shape: "
            '{"diagnosis":"","likelyCause":"","suggestedFix":{"summary":"","detail":"","targetFile":"","targetLine":0,"willModify":[],"willCreate":[],"sideEffectWarnings":[]},"questions":[]}\n\n'
            f"DEBUG CONTEXT:\n{self._skill_adapter.augment_context(debug_context, bundle)}"
        )
        payload = self._chat_structured(system=system, user=user)
        if payload is None:
            return {
                "diagnosis": "HuggingFace runtime could not analyze the debug context.",
                "likelyCause": "",
                "suggestedFix": {
                    "summary": "Check WATERFREE_HF_TOKEN and model availability.",
                    "detail": "",
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
        payload = self._chat_structured(system=system, user=user)
        if payload is None:
            text = self._chat(system=system, user=user).strip()
            return {"answer": text, "shouldUpdatePlan": False, "followupTasks": [], "questions": []}
        return {
            "answer": str(payload.get("answer", "")),
            "shouldUpdatePlan": bool(payload.get("shouldUpdatePlan", False)),
            "followupTasks": list(payload.get("followupTasks", [])),
            "designArtifacts": dict(payload.get("designArtifacts", {})),
            "questions": list(payload.get("questions", [])),
        }

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

    def triage_knowledge_symbols(
        self, *, source_repo: str, focus: str, batch: list[dict], workspace_path: str = ""
    ) -> list[int]:
        system = build_system_prompt("KNOWLEDGE", "default")
        user = (
            'Return JSON only with shape: {"selected":[<indices of relevant items>]}\n\n'
            f"SOURCE REPO: {source_repo}\nFOCUS: {focus}\n\nBATCH:\n"
            + json.dumps(batch, ensure_ascii=True)
        )
        payload = self._chat_structured(system=system, user=user)
        if payload and isinstance(payload.get("selected"), list):
            return [int(i) for i in payload["selected"] if isinstance(i, (int, float))]
        return list(range(len(batch)))

    def describe_knowledge_batch(
        self, *, source_repo: str, focus: str, batch: list[dict], workspace_path: str = ""
    ) -> list[dict]:
        system = build_system_prompt("KNOWLEDGE", "default")
        user = (
            'Return JSON only with shape: {"descriptions":[{"index":0,"summary":"","tags":[]}]}\n\n'
            f"SOURCE REPO: {source_repo}\nFOCUS: {focus}\n\nBATCH:\n"
            + json.dumps(batch, ensure_ascii=True)
        )
        payload = self._chat_structured(system=system, user=user)
        if payload and isinstance(payload.get("descriptions"), list):
            return list(payload["descriptions"])
        return []

    def summarize_procedure_knowledge(
        self, *, context: str, focus: str = "", workspace_path: str = ""
    ) -> dict:
        system = build_system_prompt("KNOWLEDGE", "default")
        user = (
            'Return JSON only with shape: {"summary":"","keyPatterns":[],"antiPatterns":[]}\n\n'
            f"FOCUS: {focus}\n\nCONTEXT:\n{context}"
        )
        payload = self._chat_structured(system=system, user=user)
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
        payload = self._chat_structured(system=system, user=user)
        return payload or {}

    # ------------------------------------------------------------------
    # Skill helpers (read-only, no side-effects)
    # ------------------------------------------------------------------

    def refresh_skills(self) -> int:
        return len(self._skill_registry.reload())

    def list_skills(self, persona: str = "", stage: str = "") -> list[dict]:
        return self._skill_registry.to_dicts(persona=persona, stage=stage)

    def get_skill_detail(self, skill_id: str) -> dict:
        return self._skill_registry.get_skill_detail(skill_id)
