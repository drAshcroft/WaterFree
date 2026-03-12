"""
Mock LLM runtime for functional testing.

Returns fixed, predictable responses based on keyword matching in the user
prompt.  No real LLM calls are made, so tests are fast and deterministic.

Usage (programmatic):
    from backend.llm.providers.mock_runtime import MockRuntime

    runtime = MockRuntime(
        rules=[
            {"match": "generate plan", "response": {"tasks": [], "questions": []}},
            {"match": "annotate",      "response": {"summary": "ok", "detail": ""}},
        ],
        default_response={"result": "default mock response"},
    )

Usage (rules file):
    runtime = MockRuntime(rules_file="/path/to/rules.json")

Rules-file format (JSON):
    {
        "default": {"result": "default"},
        "rules": [
            {"match": "keyword", "response": {...}},
            ...
        ]
    }

Rule matching is case-insensitive substring search on the *user* prompt.
The first matching rule wins.  If no rule matches, ``default_response`` is
used.  A missing ``default_response`` returns ``{}``.

Each ``response`` value may be a ``dict`` (returned as-is for structured
calls) or a JSON string (parsed for structured calls, returned raw for text
calls).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend.llm.prompt_templates import build_system_prompt
from backend.llm.structural_support import apply_task_dependencies, task_from_raw
from backend.session.models import (
    AnnotationStatus,
    CodeCoord,
    IntentAnnotation,
    Task,
)
from backend.todo.store import TaskStore
from backend.llm.checkpoints.store import CheckpointStore

_DEFAULT_TIMEOUT = 300.0
_POLL_INTERVAL = 0.5


class MockRuntime:
    """AgentRuntime that returns scripted responses from a rule table.

    When ``interactive=True`` the rule table is bypassed: each call writes a
    capture file to ``{workspace}/.waterfree/mock/`` and blocks until the
    WaterFree Mock Panel webview (or any tool) writes a matching response file.
    """

    def __init__(
        self,
        *,
        rules: Optional[list[dict]] = None,
        rules_file: Optional[str] = None,
        default_response: Optional[dict] = None,
        workspace_path: str = ".",
        interactive: bool = False,
        capture_dir: Optional[str] = None,
        timeout: Optional[float] = None,
        task_store_factory: Optional[Callable[[str], TaskStore]] = None,
        checkpoint_store_factory: Optional[Callable[[str], CheckpointStore]] = None,
    ) -> None:
        self._workspace_path = workspace_path
        self._interactive = interactive
        self._capture_dir = (
            capture_dir
            or os.path.join(workspace_path, ".waterfree", "mock")
        )
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        self._task_store_factory = task_store_factory or (lambda wp: TaskStore(wp))
        self._checkpoint_store_factory = checkpoint_store_factory or (
            lambda wp: CheckpointStore(wp)
        )
        self._task_stores: dict[str, TaskStore] = {}
        self._checkpoint_stores: dict[str, CheckpointStore] = {}
        self._in_memory_checkpoints: dict[str, dict] = {}

        if rules_file:
            self._rules, self._default_response = _load_rules_file(rules_file)
        else:
            self._rules = list(rules or [])
            self._default_response = dict(default_response or {})

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def runtime_id(self) -> str:
        return "mock"

    # ------------------------------------------------------------------
    # Interactive capture / await
    # ------------------------------------------------------------------

    def _capture_and_await(self, *, stage: str, system: str, user: str) -> str:
        """Write a capture file and block until a matching response file appears."""
        call_id = uuid.uuid4().hex[:12]
        os.makedirs(self._capture_dir, exist_ok=True)
        capture_path = os.path.join(self._capture_dir, f"capture_{call_id}.json")
        response_path = os.path.join(self._capture_dir, f"response_{call_id}.json")
        capture = {
            "id": call_id,
            "stage": stage,
            "persona": "mock",
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
        return ""

    def _capture_structured(self, *, stage: str, system: str, user: str) -> Optional[dict[str, Any]]:
        text = self._capture_and_await(stage=stage, system=system, user=user)
        if not text:
            return None
        text = text.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        import re
        for candidate in reversed(re.findall(r"\{.*\}", text, flags=re.DOTALL)):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Rule-matching helpers
    # ------------------------------------------------------------------

    def _match_dict(self, user_prompt: str) -> dict[str, Any]:
        lower = user_prompt.lower()
        for rule in self._rules:
            match_key = (rule.get("match") or "").lower()
            if match_key and match_key in lower:
                resp = rule.get("response", {})
                if isinstance(resp, str):
                    try:
                        return json.loads(resp)
                    except Exception:
                        return {"text": resp}
                return dict(resp)
        return dict(self._default_response)

    def _match_text(self, user_prompt: str) -> str:
        lower = user_prompt.lower()
        for rule in self._rules:
            match_key = (rule.get("match") or "").lower()
            if match_key and match_key in lower:
                resp = rule.get("response", {})
                if isinstance(resp, str):
                    return resp
                return json.dumps(resp, ensure_ascii=True)
        return json.dumps(self._default_response, ensure_ascii=True)

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
        user = f"GOAL:\n{goal}\n\nCONTEXT:\n{context}"
        if self._interactive:
            system = build_system_prompt("PLANNING", persona)
            payload = self._capture_structured(stage="PLANNING", system=system, user=user) or {}
        else:
            payload = self._match_dict(user)
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
        user = f"TASK TITLE: {task.title}\nTASK DESCRIPTION: {task.description}\n\nCONTEXT:\n{context}"
        if self._interactive:
            system = build_system_prompt("ANNOTATION", persona)
            payload = self._capture_structured(stage="ANNOTATION", system=system, user=user) or {}
        else:
            payload = self._match_dict(user)
        return IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(
                file=task.target_file, line=task.target_line, method=task.target_function
            ),
            summary=str(payload.get("summary", "Mock annotation summary.")),
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
        user = f"TASK TITLE: {task.title}\nTASK DESCRIPTION: {task.description}\n\nCONTEXT:\n{context}"
        if self._interactive:
            system = build_system_prompt("EXECUTION", persona)
            payload = self._capture_structured(stage="EXECUTION", system=system, user=user) or {}
        else:
            payload = self._match_dict(user)
        return list(payload.get("edits", []))

    def detect_ripple(self, task: Task, scan_context: str, workspace_path: str = "") -> str:
        user = f"TASK TITLE: {task.title}\n\nSCAN:\n{scan_context}"
        if self._interactive:
            system = build_system_prompt("RIPPLE_DETECTION", "reviewer")
            payload = self._capture_structured(stage="RIPPLE_DETECTION", system=system, user=user) or {}
        else:
            payload = self._match_dict(user)
        return str(payload.get("summary", ""))

    def alter_annotation(
        self,
        task: Task,
        old_annotation: IntentAnnotation,
        feedback: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> IntentAnnotation:
        user = (
            f"TASK TITLE: {task.title}\nDEVELOPER FEEDBACK: {feedback}\n\n"
            f"PREVIOUS SUMMARY: {old_annotation.summary}\n\nCONTEXT:\n{context}"
        )
        if self._interactive:
            system = build_system_prompt("ALTER_ANNOTATION", persona)
            payload = self._capture_structured(stage="ALTER_ANNOTATION", system=system, user=user) or {}
        else:
            payload = self._match_dict(user)
        return IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(
                file=task.target_file, line=task.target_line, method=task.target_function
            ),
            summary=str(payload.get("summary", old_annotation.summary or "")),
            detail=str(payload.get("detail", old_annotation.detail or "")),
            approach=str(payload.get("approach", old_annotation.approach or "")),
            will_create=list(payload.get("willCreate", list(old_annotation.will_create))),
            will_modify=list(payload.get("willModify", list(old_annotation.will_modify))),
            will_delete=list(payload.get("willDelete", list(old_annotation.will_delete))),
            side_effect_warnings=list(
                payload.get("sideEffectWarnings", list(old_annotation.side_effect_warnings))
            ),
            assumptions_made=list(
                payload.get("assumptionsMade", list(old_annotation.assumptions_made))
            ),
            questions_before_proceeding=list(
                payload.get(
                    "questionsBeforeProceeding",
                    list(old_annotation.questions_before_proceeding),
                )
            ),
            status=AnnotationStatus.PENDING,
        )

    def analyze_debug_context(
        self,
        debug_context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> dict:
        user = f"DEBUG CONTEXT:\n{debug_context}"
        if self._interactive:
            system = build_system_prompt("LIVE_DEBUG", persona)
            payload = self._capture_structured(stage="LIVE_DEBUG", system=system, user=user) or {}
        else:
            payload = self._match_dict(user)
        suggested_fix = dict(payload.get("suggestedFix", {}))
        return {
            "diagnosis": str(payload.get("diagnosis", "Mock diagnosis.")),
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
        user = f"QUESTION: {question}\n\nCONTEXT:\n{context}"
        if self._interactive:
            system = build_system_prompt("QUESTION_ANSWER", persona)
            payload = self._capture_structured(stage="QUESTION_ANSWER", system=system, user=user) or {}
        else:
            payload = self._match_dict(user)
        return {
            "answer": str(payload.get("answer", "Mock answer.")),
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
        user = f"SOURCE REPO: {source_repo}\nFOCUS: {focus}\nBATCH size: {len(batch)}"
        payload = self._match_dict(user)
        if isinstance(payload.get("selected"), list):
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
        user = f"SOURCE REPO: {source_repo}\nFOCUS: {focus}\nBATCH size: {len(batch)}"
        payload = self._match_dict(user)
        if isinstance(payload.get("descriptions"), list):
            return list(payload["descriptions"])
        return []

    def summarize_procedure_knowledge(
        self,
        *,
        context: str,
        focus: str = "",
        workspace_path: str = "",
    ) -> dict:
        user = f"FOCUS: {focus}\n\nCONTEXT:\n{context}"
        return self._match_dict(user)

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
        user = f"STAGE KIND: {stage_kind}\nSTAGE TITLE: {stage_title}\nGOAL:\n{goal}"
        if self._interactive:
            system = build_system_prompt("WIZARD", persona)
            return self._capture_structured(stage="WIZARD", system=system, user=user) or {}
        return self._match_dict(user)

    # ------------------------------------------------------------------
    # Checkpoint / resume (in-memory)
    # ------------------------------------------------------------------

    def checkpoint(self, session_id: str, reason: str, payload: dict) -> dict:
        cp_id = str(uuid.uuid4())
        record = {
            "id": cp_id,
            "sessionId": session_id,
            "reason": reason,
            "runtimeId": self.runtime_id,
            "payload": payload,
            "status": "pending",
        }
        self._in_memory_checkpoints[cp_id] = record
        return record

    def resume(self, checkpoint_id: str, decision: dict) -> dict:
        cp = self._in_memory_checkpoints.get(checkpoint_id)
        if cp is None:
            raise ValueError(f"Checkpoint not found: {checkpoint_id}")
        cp["decision"] = decision
        cp["status"] = "resumed"
        return cp


# ---------------------------------------------------------------------------
# Rules-file loader
# ---------------------------------------------------------------------------

def _load_rules_file(path: str) -> tuple[list[dict], dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data, {}
    rules = list(data.get("rules", []))
    default = dict(data.get("default", {}))
    return rules, default
