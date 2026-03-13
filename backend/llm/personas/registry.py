"""
Persona registry — lookup, fragment assembly, and serialization helpers.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class PersonaDef:
    id: str
    name: str
    icon: str       # short label for the sidebar chip button
    tagline: str    # one-line description shown in the UI picker
    system_fragment: str  # injected into all stage system prompts
    stage_fragments: dict[str, str] = field(default_factory=dict)


# Populated by each persona module at import time
PERSONAS: dict[str, PersonaDef] = {}

DEFAULT_PERSONA = "architect"

# Neutral/no-op fragment used when persona is "default" or unknown
_NO_OP_FRAGMENT = ""


def _register(*personas: PersonaDef) -> None:
    for persona in personas:
        PERSONAS[persona.id] = persona


def get_persona_fragment(persona_id: str, stage: str = "", prompt_override: str = "") -> str:
    """Return the persona fragment for a stage, or empty string for unknown ids."""
    persona = PERSONAS.get(persona_id)
    if not persona:
        return _NO_OP_FRAGMENT
    base_fragment = prompt_override.strip() or persona.system_fragment
    parts = [base_fragment]
    stage_fragment = persona.stage_fragments.get(stage.upper())
    if stage_fragment:
        parts.append(stage_fragment)
    return "\n".join(part for part in parts if part)


def list_personas() -> list[dict]:
    """Return a serializable list of all personas for the frontend."""
    return [
        {
            "id": p.id,
            "name": p.name,
            "icon": p.icon,
            "tagline": p.tagline,
            "systemFragment": p.system_fragment,
            "stageFragments": dict(p.stage_fragments),
        }
        for p in PERSONAS.values()
    ]
