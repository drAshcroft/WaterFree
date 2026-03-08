"""
Skill registry with persona/stage filtering and detail loading.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .loader import SkillInfo, discover_skills


class SkillRegistry:
    def __init__(self, workspace_path: str, extra_roots: list[str] | None = None):
        self._workspace_path = workspace_path
        self._extra_roots = list(extra_roots or [])
        self._skills: dict[str, SkillInfo] = {}
        self.reload()

    def reload(self) -> list[SkillInfo]:
        discovered = discover_skills(self._workspace_path, extra_roots=self._extra_roots)
        self._skills = {skill.id: skill for skill in discovered}
        return discovered

    def list_skills(self, persona: str = "", stage: str = "") -> list[SkillInfo]:
        persona_key = persona.strip().lower()
        stage_key = stage.strip().lower()
        selected: list[SkillInfo] = []
        for skill in self._skills.values():
            if self._matches(skill, persona_key, stage_key):
                selected.append(skill)
        return sorted(selected, key=lambda skill: skill.id)

    def get_skill(self, skill_id: str) -> Optional[SkillInfo]:
        return self._skills.get(skill_id)

    def get_skill_detail(self, skill_id: str) -> dict:
        skill = self.get_skill(skill_id)
        if not skill:
            raise ValueError(f"Unknown skill: {skill_id}")
        skill_path = Path(skill.path)
        skill_dir = skill_path.parent
        markdown = skill_path.read_text(encoding="utf-8")
        references = _list_files(skill_dir / "references")
        scripts = _list_files(skill_dir / "scripts")
        return {
            "markdown": markdown,
            "references": references,
            "scripts": scripts,
        }

    def to_dicts(self, persona: str = "", stage: str = "") -> list[dict]:
        return [skill.to_dict() for skill in self.list_skills(persona=persona, stage=stage)]

    def _matches(self, skill: SkillInfo, persona: str, stage: str) -> bool:
        tokens = {token.lower() for token in skill.applies_to}
        if not persona and not stage:
            return True
        if persona and persona in tokens:
            return True
        if stage and stage in tokens:
            return True
        skill_id = skill.id.lower()
        if persona and persona in skill_id:
            return True
        if stage and stage in skill_id:
            return True
        return not tokens or "general" in tokens


def _list_files(path: Path) -> list[str]:
    if not path.exists():
        return []
    return sorted(
        str(child.resolve())
        for child in path.glob("**/*")
        if child.is_file()
    )
