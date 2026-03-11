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
from backend.llm.structural_support import route_structural_persona
from backend.wizard.design_artifacts import normalize_design_artifacts


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
        effective_persona = route_structural_persona(
            persona,
            "planning",
            stage_kind,
            stage_title,
            goal,
            context,
            json.dumps(metadata, ensure_ascii=True),
        )
        bundle = self._skill_adapter.select(
            persona=_normalize_persona(effective_persona), stage="planning"
        )
        web_tools = bool(metadata.get("webToolsEnabled"))
        coding_stage_rules = ""
        if stage_kind == "coding_agents":
            coding_stage_rules = (
                "- For coding_agents, emit a concrete implementation backlog unless the context is truly empty.\n"
                "- For coding_agents, prefer several smaller todos over one generic build item, and target files, classes, and procedures whenever the context supports it.\n"
                "- For coding_agents, include test work, cleanup/refactor work, and review or spike follow-ups when upstream guidance is vague, contradictory, or not implementable.\n"
                "- For coding_agents, use `questions` for focused human clarifications instead of inventing missing interfaces or behavior.\n"
            )
        prompt = (
            "Return JSON only with shape: "
            '{"stageSummary":"","chunks":[{"id":"","content":""}],"todos":[{"id":"","title":"","description":"","prompt":"","rationale":"","phase":"","priority":"P0|P1|P2|P3|spike","taskType":"impl|test|spike|review|refactor","targetFile":"","targetFunction":"","contextCoords":[{"file":"","class":"","method":"","line":0,"anchorType":"modify"}],"dependsOn":[{"taskId":"","title":"","type":"blocks|informs|shares-file"}],"ownerType":"human|agent|unassigned","ownerName":"","estimatedMinutes":0,"aiNotes":""}],"subsystems":[],"designArtifacts":{"subsystems":[],"interfaces":[],"interfaceMethods":[],"dataContracts":[],"apiCatalog":[],"patternChoices":[],"antiPatterns":[],"integrationPolicies":[],"todos":[]},"externalResearchPrompt":"","questions":[]}\n\n'
            f"STAGE KIND: {stage_kind}\n"
            f"STAGE TITLE: {stage_title}\n"
            f"GOAL: {goal}\n"
            f"EFFECTIVE PERSONA: {effective_persona}\n"
            f"WEB TOOLS AVAILABLE: {'yes' if web_tools else 'no'}\n"
            f"REVISION REQUEST: {revision_request.strip() or '(none)'}\n"
            f"METADATA: {json.dumps(metadata, ensure_ascii=True)}\n"
            f"CHUNKS TO DRAFT: {json.dumps(chunk_specs, ensure_ascii=True)}\n\n"
            "Rules:\n"
            "- Draft only the requested chunk ids.\n"
            "- Preserve the stage intent and produce concise markdown-ready prose.\n"
            "- Emit todo items only when the stage naturally produces follow-up work.\n"
            "- For architect review, include a realistic `subsystems` list and macro risks.\n"
            "- For design pattern work, prefer structured design artifacts over vague prose.\n"
            "- When you emit todos, fill rationale, dependencies, context coordinates, effort, and confidence notes whenever the information is available.\n"
            "- For market research without web tools, provide an `externalResearchPrompt`.\n\n"
            f"{coding_stage_rules}"
            f"CONTEXT:\n{self._skill_adapter.augment_context(context, bundle)}"
        )
        prompt_stage = "QUESTION_ANSWER" if stage_kind == "review" else "PLANNING"
        payload = self._executor._run_deepagents_structured(
            stage=prompt_stage,
            prompt=prompt,
            workspace_path=workspace_path,
            persona=effective_persona,
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
        design_artifacts: dict[str, Any] = {}
        if stage_kind == "architect_review":
            todos.append({
                "title": "Turn architect output into subsystem work",
                "description": f"Convert the accepted architect review for '{goal}' into subsystem plans.",
                "rationale": "Accepted architecture should be decomposed into subsystem-level contracts before coding starts.",
                "phase": stage_title,
                "priority": "P1",
                "taskType": "spike",
                "targetFile": "",
                "targetFunction": "",
                "ownerType": "unassigned",
                "ownerName": "",
                "estimatedMinutes": 30,
                "aiNotes": "Start with subsystem boundaries, then define interfaces and integration policy.",
                "prompt": "Use the accepted architect chunks to define subsystem work.",
            })
            design_artifacts = normalize_design_artifacts(
                {
                    "designArtifacts": {
                        "subsystems": [{"name": "Core Application"}, {"name": "API Layer"}, {"name": "Data Layer"}],
                        "patternChoices": [{"name": "Subsystem-first breakdown"}],
                        "integrationPolicies": [{"name": "Architect hands structural decomposition to pattern expert"}],
                    }
                },
                fallback_subsystems=["Core Application", "API Layer", "Data Layer"],
            )
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
            todos.extend([
                {
                    "title": "Validate implementation contracts",
                    "description": f"Review the accepted coding handoff for '{goal}' and confirm which interfaces, data contracts, and assumptions are stable enough to implement.",
                    "rationale": "Implementation should start from interfaces that survive contact with the codebase.",
                    "phase": stage_title,
                    "priority": "P1",
                    "taskType": "review",
                    "targetFile": "",
                    "targetFunction": "",
                    "ownerType": "unassigned",
                    "ownerName": "",
                    "estimatedMinutes": 20,
                    "aiNotes": "Escalate contradictions or impossible guidance before committing broad code changes.",
                    "prompt": "Validate upstream guidance against the real codebase and call out mismatches.",
                },
                {
                    "title": f"Implement core procedures for {goal}",
                    "description": "Build the main developer-owned behavior in the accepted execution order.",
                    "rationale": "Core implementation should be expressed as explicit coding work instead of a single generic build item.",
                    "phase": stage_title,
                    "priority": "P1",
                    "taskType": "impl",
                    "targetFile": "",
                    "targetFunction": "",
                    "ownerType": "unassigned",
                    "ownerName": "",
                    "estimatedMinutes": 60,
                    "aiNotes": "Prefer concrete procedures, classes, and adapters with small follow-through steps.",
                    "prompt": "Implement the core procedures and classes identified in the accepted coding handoff.",
                },
                {
                    "title": f"Wire integrations and interface fixes for {goal}",
                    "description": "Connect the implemented behavior to its surrounding adapters, and correct interface mismatches uncovered during coding.",
                    "rationale": "Real implementation often exposes interface problems that need explicit follow-through.",
                    "phase": stage_title,
                    "priority": "P1",
                    "taskType": "refactor",
                    "targetFile": "",
                    "targetFunction": "",
                    "ownerType": "unassigned",
                    "ownerName": "",
                    "estimatedMinutes": 45,
                    "aiNotes": "If an upstream design decision is wrong, document the failure and route a review or spike task.",
                    "prompt": "Wire integrations, fix mismatched interfaces, and keep implementation aligned with the viable design.",
                },
                {
                    "title": f"Verify behavior for {goal}",
                    "description": "Implement or update tests that prove the accepted BDD and interface behavior still holds after coding.",
                    "rationale": "Implementation is incomplete until the relevant behavior is verified.",
                    "phase": stage_title,
                    "priority": "P1",
                    "taskType": "test",
                    "targetFile": "",
                    "targetFunction": "",
                    "ownerType": "unassigned",
                    "ownerName": "",
                    "estimatedMinutes": 30,
                    "aiNotes": "Route failing expectations back upstream when the accepted design cannot be satisfied cleanly.",
                    "prompt": "Write or update tests that prove the new code and interfaces behave as intended.",
                },
            ])

        subsystems: list[Any] = []
        if stage_kind == "architect_review":
            subsystems = ["Core Application", "API Layer", "Data Layer"]
        elif stage_kind == "design_pattern_agent":
            subsystem_name = metadata.get("subsystemName") or stage_title
            design_artifacts = normalize_design_artifacts(
                {
                    "designArtifacts": {
                        "subsystems": [{
                            "name": subsystem_name,
                            "purpose": f"Own the {subsystem_name} behavior and its contracts.",
                            "boundaries": "Translate external behavior at the subsystem boundary.",
                            "failureModes": "Fail explicitly and preserve upstream contracts.",
                        }],
                        "interfaces": [{"name": f"{subsystem_name} contract", "owner": subsystem_name}],
                        "interfaceMethods": [{"name": "handle", "interface": f"{subsystem_name} contract"}],
                        "dataContracts": [{"name": f"{subsystem_name} input"}],
                        "apiCatalog": [],
                        "patternChoices": [{"name": "Explicit interface ownership"}],
                        "antiPatterns": [{"name": "Cross-layer leakage"}],
                        "integrationPolicies": [{"name": "Wrap third-party failures behind local contracts"}],
                    }
                },
                fallback_subsystems=[str(subsystem_name)],
            )

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
            "designArtifacts": design_artifacts,
            "externalResearchPrompt": external_prompt,
            "questions": [],
        }
