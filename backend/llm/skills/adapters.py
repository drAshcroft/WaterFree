"""
Runtime-facing skill adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.llm.personas import get_persona

from .loader import SkillInfo
from .registry import SkillRegistry


@dataclass(frozen=True)
class SkillBundle:
    skills: list[SkillInfo] = field(default_factory=list)
    system_note: str = ""
    preferred_tool_categories: list[str] = field(default_factory=list)

    @property
    def skill_ids(self) -> list[str]:
        return [skill.id for skill in self.skills]


class SkillAdapter:
    def __init__(self, registry: SkillRegistry):
        self._registry = registry

    def select(self, *, persona: str, stage: str, task_type: str = "") -> SkillBundle:
        stage_keys = _expanded_stage_keys(persona=persona, stage=stage)
        selected: list[SkillInfo] = []
        persona_def = get_persona(persona)
        if persona_def is not None:
            for skill_id in persona_def.preferred_skill_ids:
                skill = self._registry.get_skill(skill_id)
                if skill is not None:
                    selected.append(skill)
        for stage_key in stage_keys:
            selected.extend(self._registry.list_skills(persona=persona, stage=stage_key))
        selected = _dedupe_skills(selected)
        category_order: list[str] = []
        for skill in selected:
            sid = skill.id.lower()
            if "debug" in sid:
                category_order.extend(["debug", "graph"])
            if "index" in sid:
                category_order.extend(["graph"])
            if "knowledge" in sid:
                category_order.extend(["knowledge"])
            if "todo" in sid:
                category_order.extend(["backlog"])
            if "filesystem" in sid:
                category_order.extend(["filesystem"])
        if task_type.lower() == "test":
            category_order.append("filesystem")

        note = self._build_note(selected, persona=persona, stage=stage)
        return SkillBundle(
            skills=selected,
            system_note=note,
            preferred_tool_categories=_unique_preserving_order(category_order),
        )

    def augment_system_prompt(self, system_prompt: str, bundle: SkillBundle) -> str:
        if not bundle.system_note:
            return system_prompt
        return f"{system_prompt}\n\n{bundle.system_note}"

    def augment_context(self, context: str, bundle: SkillBundle) -> str:
        if not bundle.skills:
            return context
        skill_list = ", ".join(bundle.skill_ids)
        return f"{context}\n\nACTIVE_SKILLS: {skill_list}"

    def _build_note(self, skills: list[SkillInfo], *, persona: str, stage: str) -> str:
        if not skills:
            return ""
        lines = [
            "Skill guidance (progressive disclosure):",
            f"- Persona: {persona or 'default'}",
            f"- Stage: {stage or 'general'}",
        ]
        for skill in skills[:6]:
            lines.append(f"- {skill.id}: {skill.description}")
        return "\n".join(lines)


def _unique_preserving_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _dedupe_skills(skills: list[SkillInfo]) -> list[SkillInfo]:
    ordered: list[SkillInfo] = []
    seen: set[str] = set()
    for skill in skills:
        if skill.id in seen:
            continue
        seen.add(skill.id)
        ordered.append(skill)
    return ordered


def _expanded_stage_keys(*, persona: str, stage: str) -> list[str]:
    keys = [stage]
    stage_key = stage.strip().lower()
    persona_key = persona.strip().lower()
    if stage_key in {"annotation", "alter_annotation", "question_answer"}:
        keys.extend(["analysis", "architecture"])
    if stage_key in {"planning", "annotation", "alter_annotation", "question_answer"}:
        keys.append("planning")
    if persona_key == "pattern_expert":
        keys.extend(["analysis", "architecture", "planning", "research"])
    return _unique_preserving_order([key for key in keys if key])
