from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.wizard.definitions import MARKET_RESEARCH_TEMPLATE
from backend.wizard.models import (
    WizardChunkStatus,
    WizardRun,
    WizardStageState,
    WizardStageStatus,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_goal_text(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip()


def _external_market_research_prompt(goal: str) -> str:
    return (
        "Research this software idea on the live web and return a concise market memo.\n\n"
        f"Idea: {goal}\n\n"
        "Populate these sections:\n"
        "- Similar Ideas and Niches: comparable products, adjacent niches, what looks strong, weak, or differentiated\n"
        "- Who Wants This?: likely audiences, pains, urgency, and why they would care\n"
        "\nAlso cover:\n"
        "- realistic MVP\n"
        "- pricing or monetization signals if visible\n"
        "- risks or reasons the idea may fail\n"
    )


def _phase_for_stage(stage_id: str) -> int:
    from backend.wizard.definitions import (
        ARCHITECT_TEMPLATE,
        BDD_TEMPLATE,
        CODING_TEMPLATE,
        MARKET_RESEARCH_TEMPLATE,
        REVIEW_TEMPLATE,
    )
    if stage_id == MARKET_RESEARCH_TEMPLATE.id:
        return 1
    if stage_id == ARCHITECT_TEMPLATE.id:
        return 2
    if stage_id.startswith("design:"):
        return 3
    if stage_id.startswith("wireframe:"):
        return 4
    if stage_id == BDD_TEMPLATE.id:
        return 5
    if stage_id == CODING_TEMPLATE.id:
        return 6
    if stage_id == REVIEW_TEMPLATE.id:
        return 7
    return 99


class StageExecutor:
    """Handles LLM calls per stage/chunk, context building, and result merging."""

    def build_stage_context(
        self,
        run: WizardRun,
        stage: WizardStageState,
        *,
        extra_context: str = "",
    ) -> str:
        parts = [
            f"GOAL:\n{run.goal}",
            "",
            "ACCEPTED CONTEXT:",
        ]
        for other in run.stages:
            if _phase_for_stage(other.id) > _phase_for_stage(stage.id):
                continue
            accepted_chunks = [chunk for chunk in other.chunks if chunk.status == WizardChunkStatus.ACCEPTED]
            if not accepted_chunks:
                continue
            parts.append(f"## {other.title}")
            for chunk in accepted_chunks:
                parts.append(f"### {chunk.title}")
                parts.append(chunk.accepted_text.strip())
                parts.append("")
        if extra_context.strip():
            parts.extend(["EXTRA CONTEXT:", extra_context.strip(), ""])
        return "\n".join(parts).strip()

    def call_runtime(
        self,
        *,
        runtime,
        run: WizardRun,
        stage: WizardStageState,
        chunk_specs: list[dict],
        revision_note: str = "",
        extra_context: str = "",
    ) -> dict:
        stage_context = self.build_stage_context(run, stage, extra_context=extra_context)
        run_stage_fn = getattr(runtime, "run_wizard_stage", None)
        payload: Optional[dict] = None
        if callable(run_stage_fn):
            payload = run_stage_fn(
                stage_kind=stage.kind,
                stage_title=stage.title,
                goal=run.goal,
                context=stage_context,
                chunk_specs=chunk_specs,
                workspace_path=run.workspace_path,
                persona=stage.persona,
                revision_request=revision_note,
                metadata={
                    "subsystemName": stage.subsystem_name,
                    "webToolsEnabled": bool(os.environ.get("WATERFREE_ENABLE_WEB_TOOLS", "").strip()),
                },
            )
        if not isinstance(payload, dict):
            payload = self.fallback_stage_payload(run, stage, chunk_specs, revision_note)
        return payload

    def fallback_stage_payload(
        self,
        run: WizardRun,
        stage: WizardStageState,
        chunk_specs: list[dict],
        revision_note: str,
    ) -> dict:
        chunks = []
        todos = []
        for spec in chunk_specs:
            note_text = str(spec.get("notes", "")).strip()
            body_lines = [
                f"{stage.title} draft for {run.goal}.",
                "",
                f"Focus: {spec['title']}.",
            ]
            if note_text:
                body_lines.extend(["", "Current notes:", note_text])
            if revision_note.strip():
                body_lines.extend(["", "Revision request:", revision_note.strip()])
            chunks.append({
                "id": spec["id"],
                "content": "\n".join(body_lines).strip(),
            })

        if stage.kind == "architect_review":
            todos.append({
                "title": "Architecture roadmap follow-up",
                "description": f"Turn the accepted architect outputs for '{run.goal}' into executable design work.",
                "priority": "P1",
                "taskType": "spike",
                "phase": stage.title,
            })
        elif stage.kind == "design_pattern_agent":
            todos.append({
                "title": f"Design subsystem: {stage.subsystem_name or stage.title}",
                "description": f"Refine interfaces and data flow for {stage.subsystem_name or stage.title}.",
                "priority": "P1",
                "taskType": "spike",
                "phase": stage.title,
            })
        elif stage.kind == "wireframe_agents":
            todos.append({
                "title": f"Wireframe subsystem: {stage.subsystem_name or stage.title}",
                "description": f"Create coding prompts and scaffolding guidance for {stage.subsystem_name or stage.title}.",
                "priority": "P1",
                "taskType": "impl",
                "phase": stage.title,
            })
        elif stage.kind == "bdd_ai_tests":
            todos.append({
                "title": "BDD acceptance coverage",
                "description": f"Write the accepted BDD scenarios for '{run.goal}'.",
                "priority": "P1",
                "taskType": "test",
                "phase": stage.title,
            })
        elif stage.kind == "coding_agents":
            todos.append({
                "title": "Implement accepted micro-prompts",
                "description": f"Build the accepted implementation tasks for '{run.goal}'.",
                "priority": "P1",
                "taskType": "impl",
                "phase": stage.title,
            })

        external_prompt = ""
        if stage.kind == "market_research":
            external_prompt = _external_market_research_prompt(run.goal)

        subsystems: list[str] = []
        if stage.kind == "architect_review":
            subsystems = ["Core Application", "API Layer", "Data Layer"]

        return {
            "stageSummary": f"{stage.title} drafted for {run.goal}.",
            "chunks": chunks,
            "todos": todos,
            "subsystems": subsystems,
            "externalResearchPrompt": external_prompt,
            "questions": [],
        }
