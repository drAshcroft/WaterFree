"""
Wizard stage execution for the Deep Agents runtime.

Owns: run_wizard_stage and its fallback helper _fallback_wizard_stage.
These are wizard-specific — they produce staged content drafts for
the interactive project wizard flow.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from backend.llm.providers.task_executor import TaskExecutor, _normalize_persona


class WizardStageRunner:
    """Runs wizard stage prompts and produces structured stage payloads."""

    def __init__(self, *, executor: TaskExecutor, skill_adapter) -> None:
        self._executor = executor
        self._skill_adapter = skill_adapter

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
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(persona), stage="planning"
        )
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
        payload = self._executor._run_deepagents_structured(
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

        subsystems: list[Any] = []
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
