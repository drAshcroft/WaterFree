"""
File-backed AI persona definitions for WaterFree.
"""

from backend.llm.personas.registry import (  # noqa: F401
    DEFAULT_PERSONA,
    PERSONAS,
    PersonaDef,
    PersonaSubagentDef,
    get_persona,
    get_persona_fragment,
    list_personas,
    persona_catalog_root,
    reload_personas,
    save_persona_documents,
)
