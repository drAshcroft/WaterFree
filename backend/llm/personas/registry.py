"""
Persona registry backed by a global SKILL.md catalog in AppData.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PERSONA = "architect"
PERSONA_METADATA_FILENAME = "waterfree.persona.json"
_BUNDLED_ROOT = Path(__file__).resolve().parent / "bundled"


@dataclass(frozen=True)
class PersonaSubagentDef:
    enabled: bool = False
    description: str = ""
    prompt_stage: str = "PLANNING"


@dataclass
class PersonaDef:
    id: str
    name: str
    icon: str
    tagline: str
    system_fragment: str
    stage_fragments: dict[str, str] = field(default_factory=dict)
    preferred_model_tiers: dict[str, list[str]] = field(default_factory=dict)
    tool_categories: list[str] = field(default_factory=list)
    preferred_skill_ids: list[str] = field(default_factory=list)
    subagent: PersonaSubagentDef = field(default_factory=PersonaSubagentDef)
    skill_markdown: str = ""
    skill_path: str = ""
    metadata_path: str = ""
    waterfree_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        metadata = dict(self.waterfree_metadata)
        metadata.setdefault("version", 1)
        metadata.setdefault("id", self.id)
        metadata.setdefault("name", self.name)
        metadata.setdefault("icon", self.icon)
        metadata.setdefault("tagline", self.tagline)
        metadata.setdefault("preferredModelTiers", self.preferred_model_tiers)
        metadata.setdefault("toolCategories", self.tool_categories)
        metadata.setdefault("preferredSkillIds", self.preferred_skill_ids)
        metadata.setdefault("subagent", {
            "enabled": self.subagent.enabled,
            "description": self.subagent.description,
            "promptStage": self.subagent.prompt_stage,
        })
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "tagline": self.tagline,
            "systemFragment": self.system_fragment,
            "stageFragments": dict(self.stage_fragments),
            "preferredModelTiers": dict(self.preferred_model_tiers),
            "toolCategories": list(self.tool_categories),
            "preferredSkillIds": list(self.preferred_skill_ids),
            "subagent": {
                "enabled": self.subagent.enabled,
                "description": self.subagent.description,
                "promptStage": self.subagent.prompt_stage,
            },
            "skillMarkdown": self.skill_markdown,
            "skillPath": self.skill_path,
            "metadataPath": self.metadata_path,
            "metadataJson": json.dumps(metadata, indent=2) + "\n",
            "waterfreeMetadata": metadata,
        }


PERSONAS: dict[str, PersonaDef] = {}

# Neutral/no-op fragment used when persona is "default" or unknown.
_NO_OP_FRAGMENT = ""


def persona_catalog_root() -> Path:
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "WaterFree" / "personas"
    return Path.home() / "AppData" / "Roaming" / "WaterFree" / "personas"


def reload_personas(force_seed: bool = False) -> dict[str, PersonaDef]:
    root = persona_catalog_root()
    _seed_catalog_if_needed(root, force_seed=force_seed)

    loaded: dict[str, PersonaDef] = {}
    if root.exists():
        for skill_md in sorted(root.glob("*/SKILL.md")):
            try:
                persona = _load_persona_dir(skill_md.parent)
            except Exception as exc:
                log.warning("Skipping invalid persona folder '%s': %s", skill_md.parent, exc)
                continue
            loaded[persona.id] = persona

    if not loaded:
        _seed_catalog_if_needed(root, force_seed=True)
        for skill_md in sorted(root.glob("*/SKILL.md")):
            try:
                persona = _load_persona_dir(skill_md.parent)
            except Exception as exc:
                log.warning("Skipping invalid seeded persona folder '%s': %s", skill_md.parent, exc)
                continue
            loaded[persona.id] = persona

    PERSONAS.clear()
    PERSONAS.update(loaded)
    return PERSONAS


def get_persona(persona_id: str) -> PersonaDef | None:
    _ensure_loaded()
    return PERSONAS.get((persona_id or "").strip().lower())


def get_persona_fragment(persona_id: str, stage: str = "", prompt_override: str = "") -> str:
    """Return the persona fragment for a stage, or empty string for unknown ids."""
    _ = prompt_override
    persona = get_persona(persona_id)
    if not persona:
        return _NO_OP_FRAGMENT
    parts = [persona.system_fragment]
    stage_fragment = persona.stage_fragments.get(stage.upper())
    if stage_fragment:
        parts.append(stage_fragment)
    return "\n".join(part for part in parts if part)


def list_personas() -> list[dict[str, Any]]:
    """Return a serializable list of all personas for the frontend."""
    _ensure_loaded()
    return [persona.to_dict() for _, persona in sorted(PERSONAS.items())]


def save_persona_documents(personas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(personas, list) or not personas:
        raise ValueError("personas must be a non-empty list")

    root = persona_catalog_root()
    root.mkdir(parents=True, exist_ok=True)
    saved_ids: list[str] = []
    for raw in personas:
        if not isinstance(raw, dict):
            continue
        skill_markdown = str(raw.get("skillMarkdown", "") or "").replace("\r\n", "\n").strip()
        if not skill_markdown:
            raise ValueError("skillMarkdown is required for each persona")

        metadata = raw.get("waterfreeMetadata", raw.get("metadataJson", raw.get("metadata", {})))
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid metadata JSON for persona '{raw.get('personaId', '')}': {exc}") from exc
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")

        parsed = _parse_skill_markdown(skill_markdown)
        persona_id = _normalize_persona_id(
            metadata.get("id")
            or raw.get("personaId")
            or parsed["frontmatter"].get("name")
        )
        if not persona_id:
            raise ValueError("persona id is required")

        normalized_metadata = _normalize_metadata(
            metadata,
            frontmatter=parsed["frontmatter"],
            title=parsed["title"],
            persona_id=persona_id,
        )
        normalized_metadata["id"] = persona_id

        target = root / persona_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(skill_markdown.rstrip() + "\n", encoding="utf-8")
        (target / PERSONA_METADATA_FILENAME).write_text(
            json.dumps(normalized_metadata, indent=2) + "\n",
            encoding="utf-8",
        )
        saved_ids.append(persona_id)

    reload_personas()
    return [PERSONAS[persona_id].to_dict() for persona_id in saved_ids if persona_id in PERSONAS]


def _register(*personas: PersonaDef) -> None:
    for persona in personas:
        PERSONAS[persona.id] = persona


def _ensure_loaded() -> None:
    if not PERSONAS:
        reload_personas()


def _seed_catalog_if_needed(root: Path, *, force_seed: bool = False) -> None:
    if not _BUNDLED_ROOT.exists():
        return
    root.mkdir(parents=True, exist_ok=True)
    existing = {child.name for child in root.iterdir() if child.is_dir()}
    if not existing or force_seed:
        for bundled_persona in sorted(_BUNDLED_ROOT.iterdir()):
            if not bundled_persona.is_dir():
                continue
            target = root / bundled_persona.name
            if target.exists():
                continue
            shutil.copytree(bundled_persona, target)
        return
    if DEFAULT_PERSONA not in existing:
        architect_default = _BUNDLED_ROOT / DEFAULT_PERSONA
        if architect_default.exists() and not (root / DEFAULT_PERSONA).exists():
            shutil.copytree(architect_default, root / DEFAULT_PERSONA)


def _load_persona_dir(persona_dir: Path) -> PersonaDef:
    skill_path = persona_dir / "SKILL.md"
    metadata_path = persona_dir / PERSONA_METADATA_FILENAME
    if not skill_path.exists():
        raise ValueError("missing SKILL.md")
    if not metadata_path.exists():
        raise ValueError(f"missing {PERSONA_METADATA_FILENAME}")

    skill_markdown = skill_path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object")

    parsed = _parse_skill_markdown(skill_markdown)
    persona_id = _normalize_persona_id(
        metadata.get("id") or parsed["frontmatter"].get("name") or persona_dir.name
    )
    if not persona_id:
        raise ValueError("unable to resolve persona id")

    normalized_metadata = _normalize_metadata(
        metadata,
        frontmatter=parsed["frontmatter"],
        title=parsed["title"],
        persona_id=persona_id,
    )
    subagent = _normalize_subagent(normalized_metadata.get("subagent"))
    return PersonaDef(
        id=persona_id,
        name=str(normalized_metadata.get("name", "") or parsed["title"] or persona_id.replace("_", " ").title()).strip(),
        icon=str(normalized_metadata.get("icon", "") or persona_id[:4].title()).strip(),
        tagline=str(
            normalized_metadata.get("tagline", "")
            or parsed["frontmatter"].get("description", "")
        ).strip(),
        system_fragment=parsed["system_fragment"],
        stage_fragments=parsed["stage_fragments"],
        preferred_model_tiers=_normalize_preferred_model_tiers(normalized_metadata.get("preferredModelTiers")),
        tool_categories=_normalize_string_list(normalized_metadata.get("toolCategories"), lower=True),
        preferred_skill_ids=_normalize_string_list(normalized_metadata.get("preferredSkillIds"), lower=False),
        subagent=subagent,
        skill_markdown=skill_markdown,
        skill_path=str(skill_path),
        metadata_path=str(metadata_path),
        waterfree_metadata=normalized_metadata,
    )


def _parse_skill_markdown(markdown: str) -> dict[str, Any]:
    frontmatter, body = _split_frontmatter(markdown)
    title = ""
    system_lines: list[str] = []
    stage_lines: dict[str, list[str]] = {}
    current_kind = ""
    current_stage = ""

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
            continue
        if stripped.startswith("## "):
            heading = stripped[3:].strip()
            if heading == "System":
                current_kind = "system"
                current_stage = ""
                continue
            if heading.lower().startswith("stage:"):
                current_kind = "stage"
                current_stage = _normalize_stage_name(heading.split(":", 1)[1])
                stage_lines.setdefault(current_stage, [])
                continue
        if current_kind == "system":
            system_lines.append(line)
        elif current_kind == "stage" and current_stage:
            stage_lines.setdefault(current_stage, []).append(line)

    system_fragment = "\n".join(system_lines).strip()
    if not system_fragment:
        raise ValueError("SKILL.md must contain a '## System' section")
    stage_fragments = {
        stage: "\n".join(lines).strip()
        for stage, lines in stage_lines.items()
        if "\n".join(lines).strip()
    }
    return {
        "frontmatter": frontmatter,
        "title": title,
        "system_fragment": system_fragment,
        "stage_fragments": stage_fragments,
    }


def _split_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    lines = markdown.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}, markdown
    frontmatter: dict[str, str] = {}
    end_index = -1
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip()
    if end_index < 0:
        return {}, markdown
    body = "\n".join(lines[end_index + 1 :])
    return frontmatter, body


def _normalize_metadata(
    raw: dict[str, Any],
    *,
    frontmatter: dict[str, str],
    title: str,
    persona_id: str,
) -> dict[str, Any]:
    name = str(raw.get("name", "") or title or persona_id.replace("_", " ").title()).strip()
    tagline = str(raw.get("tagline", "") or frontmatter.get("description", "")).strip()
    return {
        "version": 1,
        "id": persona_id,
        "name": name,
        "icon": str(raw.get("icon", "") or name[:4]).strip() or name[:4],
        "tagline": tagline,
        "preferredModelTiers": _normalize_preferred_model_tiers(raw.get("preferredModelTiers")),
        "toolCategories": _normalize_string_list(raw.get("toolCategories"), lower=True),
        "preferredSkillIds": _normalize_string_list(raw.get("preferredSkillIds"), lower=False),
        "subagent": _normalize_subagent(raw.get("subagent"), as_dict=True),
    }


def _normalize_preferred_model_tiers(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for stage, tiers in raw.items():
        stage_key = _normalize_stage_name(stage)
        if not stage_key:
            continue
        normalized[stage_key] = _normalize_string_list(tiers, lower=True)
    return {stage: tiers for stage, tiers in normalized.items() if tiers}


def _normalize_subagent(raw: Any, *, as_dict: bool = False) -> PersonaSubagentDef | dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    payload = {
        "enabled": source.get("enabled", False) is True,
        "description": str(source.get("description", "") or "").strip(),
        "promptStage": _normalize_stage_name(source.get("promptStage") or "PLANNING"),
    }
    if as_dict:
        return payload
    return PersonaSubagentDef(
        enabled=payload["enabled"],
        description=payload["description"],
        prompt_stage=payload["promptStage"],
    )


def _normalize_string_list(raw: Any, *, lower: bool) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip()
        if not value:
            continue
        normalized = value.lower() if lower else value
        if normalized in seen:
            continue
        seen.add(normalized)
        values.append(normalized)
    return values


def _normalize_persona_id(raw: Any) -> str:
    return str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")


def _normalize_stage_name(raw: Any) -> str:
    value = str(raw or "").strip().upper()
    aliases = {
        "DEBUG": "LIVE_DEBUG",
        "QUESTION": "QUESTION_ANSWER",
    }
    return aliases.get(value, value)


reload_personas()
