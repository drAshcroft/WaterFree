"""
Local SKILL.md discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SkillInfo:
    id: str
    title: str
    description: str
    path: str
    applies_to: list[str] = field(default_factory=list)
    has_scripts: bool = False
    has_references: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "path": self.path,
            "appliesTo": self.applies_to,
            "hasScripts": self.has_scripts,
            "hasReferences": self.has_references,
        }


def discover_skills(workspace_path: str, extra_roots: Iterable[str] | None = None) -> list[SkillInfo]:
    roots = [Path(workspace_path).resolve() / "skills"]
    for root in extra_roots or []:
        roots.append(Path(root).resolve())

    found: dict[str, SkillInfo] = {}
    for root in roots:
        if not root.exists():
            continue
        for skill_md in root.glob("*/SKILL.md"):
            info = _skill_from_markdown(skill_md)
            found[info.id] = info
    return sorted(found.values(), key=lambda item: item.id)


def _skill_from_markdown(skill_md: Path) -> SkillInfo:
    skill_dir = skill_md.parent
    markdown = skill_md.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter(markdown)
    heading = _first_heading(markdown)

    skill_id = str(frontmatter.get("name") or skill_dir.name).strip()
    title = heading or skill_id.replace("-", " ").replace("_", " ").title()
    description = str(frontmatter.get("description") or _first_paragraph(markdown)).strip()
    applies_to = _infer_applies_to(skill_id, description, frontmatter)
    has_scripts = (skill_dir / "scripts").exists() and any((skill_dir / "scripts").iterdir())
    has_references = (skill_dir / "references").exists() and any((skill_dir / "references").iterdir())
    return SkillInfo(
        id=skill_id,
        title=title,
        description=description,
        path=str(skill_md),
        applies_to=applies_to,
        has_scripts=has_scripts,
        has_references=has_references,
    )


def _parse_frontmatter(markdown: str) -> dict[str, str]:
    lines = markdown.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}
    frontmatter: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip()
    return frontmatter


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        text = line.strip()
        if text.startswith("#"):
            return text.lstrip("# ").strip()
    return ""


def _first_paragraph(markdown: str) -> str:
    in_frontmatter = False
    lines: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if not stripped:
            if lines:
                break
            continue
        if stripped.startswith("#"):
            continue
        lines.append(stripped)
    return " ".join(lines)


def _infer_applies_to(skill_id: str, description: str, frontmatter: dict[str, str]) -> list[str]:
    explicit = frontmatter.get("applies_to") or frontmatter.get("appliesTo") or ""
    if explicit:
        return [token.strip() for token in explicit.split(",") if token.strip()]

    text = f"{skill_id} {description}".lower()
    applies_to: list[str] = []
    if "debug" in text:
        applies_to.extend(["debug", "investigation"])
    if "index" in text or "graph" in text:
        applies_to.extend(["planning", "architecture", "analysis"])
    if "knowledge" in text or "snippet" in text:
        applies_to.extend(["research", "reuse"])
    if "todo" in text or "backlog" in text or "task" in text:
        applies_to.extend(["planning", "execution"])
    if not applies_to:
        applies_to.append("general")
    return sorted(set(applies_to))
