"""
Plan generation and annotation for the Deep Agents runtime.

Owns: generate_plan, generate_annotation, detect_ripple, alter_annotation,
analyze_debug_context, answer_question, triage_knowledge_symbols,
describe_knowledge_batch, and summarize_procedure_knowledge.

All of these are "what should we do and why" — they produce structured
reasoning artifacts but do not write code files.
"""

from __future__ import annotations

from typing import Any

from backend.llm.structural_support import (
    apply_task_dependencies,
    route_structural_persona,
    task_from_raw,
)
from backend.session.models import AnnotationStatus, CodeCoord, IntentAnnotation, Task
from backend.llm.providers.task_executor import (
    TaskExecutor,
    _normalize_persona,
)


class PlanGenerator:
    """Produces plans, annotations, ripple analysis, and knowledge descriptions."""

    def __init__(self, *, executor: TaskExecutor, skill_adapter) -> None:
        self._executor = executor
        self._skill_adapter = skill_adapter

    # ------------------------------------------------------------------
    # AgentRuntime protocol methods
    # ------------------------------------------------------------------

    def generate_plan(
        self,
        goal: str,
        context: str,
        workspace_path: str = "",
        persona: str = "default",
    ) -> tuple[list[Task], list[str]]:
        effective_persona = route_structural_persona(persona, "planning", goal, context)
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(effective_persona), stage="planning"
        )
        prompt = (
            "Return JSON only with shape: "
            '{"tasks":[{"id":"","title":"","description":"","rationale":"","targetFile":"","targetFunction":"","priority":"P0|P1|P2|P3|spike","phase":"","taskType":"impl|test|spike|review|refactor","contextCoords":[{"file":"","class":"","method":"","line":0,"anchorType":"modify"}],"dependsOn":[{"taskId":"","title":"","type":"blocks|informs|shares-file"}],"owner":{"type":"human|agent|unassigned","name":""},"estimatedMinutes":0,"aiNotes":""}],"designArtifacts":{"subsystems":[],"interfaces":[],"interfaceMethods":[],"dataContracts":[],"apiCatalog":[],"patternChoices":[],"antiPatterns":[],"integrationPolicies":[],"todos":[]},"questions":[]}\n\n'
            f"GOAL:\n{goal}\n\nCONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._executor._run_deepagents_structured(
            stage="PLANNING",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=effective_persona,
        )
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
            persona,
            "annotation",
            task.title,
            task.description,
            context,
        )
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(effective_persona), stage="annotation"
        )
        prompt = (
            "Return JSON only with shape: "
            '{"summary":"","detail":"","willCreate":[],"willModify":[],"sideEffectWarnings":[],"assumptionsMade":[],"questionsBeforeProceeding":[]}\n\n'
            f"TASK TITLE: {task.title}\n"
            f"TASK DESCRIPTION: {task.description}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._executor._run_deepagents_structured(
            stage="ANNOTATION",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=effective_persona,
        )
        if payload is None:
            return IntentAnnotation(
                task_id=task.id,
                target_coord=CodeCoord(
                    file=task.target_file,
                    line=task.target_line,
                    method=task.target_function,
                ),
                summary="Deep Agents could not produce structured annotation output.",
                detail="Deep Agents unavailable or returned malformed output.",
                status=AnnotationStatus.PENDING,
            )
        return IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(
                file=task.target_file,
                line=task.target_line,
                method=task.target_function,
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
        payload = self._executor._run_deepagents_structured(
            stage="RIPPLE_DETECTION",
            prompt=prompt,
            workspace_path=workspace_path,
            persona="reviewer",
        )
        if isinstance(payload, dict):
            summary = str(payload.get("summary", "")).strip()
            if summary:
                return summary
        return self._executor._run_deepagents_text(
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
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(effective_persona), stage="annotation"
        )
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
        payload = self._executor._run_deepagents_structured(
            stage="ALTER_ANNOTATION",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=effective_persona,
        )
        if payload is None:
            return IntentAnnotation(
                task_id=task.id,
                target_coord=CodeCoord(
                    file=task.target_file,
                    line=task.target_line,
                    method=task.target_function,
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
                file=task.target_file,
                line=task.target_line,
                method=task.target_function,
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
        prompt = (
            "Return JSON only with shape: "
            '{"diagnosis":"","likelyCause":"","suggestedFix":{"summary":"","detail":"","targetFile":"","targetLine":0,"willModify":[],"willCreate":[],"sideEffectWarnings":[]},"questions":[]}\n\n'
            f"DEBUG CONTEXT:\n{self._skill_adapter.augment_context(debug_context, bundle)}"
        )
        payload = self._executor._run_deepagents_structured(
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
        effective_persona = route_structural_persona(
            persona,
            "question_answer",
            question,
            context,
        )
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(effective_persona), stage="question_answer"
        )
        prompt = (
            "Return JSON only with shape: "
            '{"answer":"","shouldUpdatePlan":false,"followupTasks":[],"designArtifacts":{"subsystems":[],"interfaces":[],"interfaceMethods":[],"dataContracts":[],"apiCatalog":[],"patternChoices":[],"antiPatterns":[],"integrationPolicies":[],"todos":[]},"questions":[]}\n\n'
            f"QUESTION: {question}\n\n"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        payload = self._executor._run_deepagents_structured(
            stage="QUESTION_ANSWER",
            prompt=prompt,
            workspace_path=workspace_path,
            persona=effective_persona,
        )
        if payload is None:
            text = self._executor._run_deepagents_text(
                stage="QUESTION_ANSWER",
                prompt=prompt,
                workspace_path=workspace_path,
                persona=effective_persona,
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
        focus_clause = (
            f"The user specifically wants knowledge about: {focus.strip()}"
            if focus.strip()
            else "Apply general judgement — keep broadly reusable content."
        )
        index_lines = []
        for i, sym in enumerate(batch):
            body = (sym.get("body") or sym.get("source") or "").strip()
            signature = body.split("\n")[0][:120]
            label = sym.get("label", "fn")
            name = sym.get("name", f"symbol_{i}")
            file_path = sym.get("file_path", "")
            index_lines.append(f"[{i}] {label} `{name}` — {file_path}\n    {signature}")

        prompt = (
            'Return JSON only with shape: {"selected":[0]}\n\n'
            "You are a code knowledge curator. Identify which symbols are worth fetching full source code for.\n"
            f"{focus_clause}\n\n"
            f"Symbol index from '{source_repo}' ({len(batch)} symbols):\n\n"
            f"{chr(10).join(index_lines)}\n\n"
            "Be selective and prefer reusable patterns, utilities, conventions, and non-obvious API usage."
        )
        payload = self._executor._run_deepagents_structured(
            stage="PLANNING",
            prompt=prompt,
            workspace_path=workspace_path,
            persona="pattern_expert",
        )
        selected = payload.get("selected", []) if isinstance(payload, dict) else []
        return [
            int(index)
            for index in selected
            if isinstance(index, (int, float)) and 0 <= int(index) < len(batch)
        ]

    def describe_knowledge_batch(
        self,
        *,
        source_repo: str,
        focus: str,
        batch: list[dict],
        workspace_path: str = "",
    ) -> list[dict]:
        focus_clause = (
            f"The user specifically wants knowledge about: {focus.strip()}"
            if focus.strip()
            else "Apply general judgement — keep broadly reusable content."
        )
        snippets: list[str] = []
        for i, symbol in enumerate(batch):
            body = (symbol.get("body") or symbol.get("source") or "").strip()
            if len(body) > 900:
                body = body[:900] + "\n... (truncated)"
            label = symbol.get("label", "function")
            name = symbol.get("name", f"symbol_{i}")
            file_path = symbol.get("file_path", "")
            snippets.append(f"--- [{i}] {label}: {name} ({file_path}) ---\n{body}")

        prompt = (
            "Return JSON only with shape: "
            '{"entries":[{"index":0,"keep":true,"snippet_type":"pattern","title":"","description":"","tags":[]}]}\n\n'
            "You are a code knowledge curator. For each snippet, decide whether it belongs in a reusable knowledge base.\n"
            f"{focus_clause}\n\n"
            f"Describe these {len(batch)} pre-selected snippets from '{source_repo}'.\n\n"
            f"{chr(10).join(snippets)}"
        )
        payload = self._executor._run_deepagents_structured(
            stage="PLANNING",
            prompt=prompt,
            workspace_path=workspace_path,
            persona="pattern_expert",
        )
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        return [item for item in entries if isinstance(item, dict)]

    def summarize_procedure_knowledge(
        self,
        *,
        context: str,
        focus: str = "",
        workspace_path: str = "",
    ) -> dict:
        focus_line = f"User focus: {focus.strip()}\n\n" if focus.strip() else ""
        prompt = (
            "Return JSON only with shape: "
            '{"keep":true,"snippet_type":"pattern","title":"","description":"","tags":[]}\n\n'
            "You are a code knowledge curator specialising in deep procedure analysis.\n"
            f"{focus_line}"
            "Summarize the procedure, its call chain, and the reusable technique.\n\n"
            f"{context}"
        )
        payload = self._executor._run_deepagents_structured(
            stage="PLANNING",
            prompt=prompt,
            workspace_path=workspace_path,
            persona="pattern_expert",
        )
        return payload if isinstance(payload, dict) else {}
