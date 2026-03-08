"""
AI persona definitions for WaterFree.

Sub-modules register personas at import time via _register().
Import this package to get all personas loaded.
"""

# Load all persona definitions (side-effect: populates PERSONAS registry)
import backend.llm.personas.planning_personas    # noqa: F401
import backend.llm.personas.development_personas  # noqa: F401
import backend.llm.personas.workflow_personas     # noqa: F401

# Re-export the public API so callers don't need to know the internal layout
from backend.llm.personas.registry import (  # noqa: F401
    PersonaDef,
    PERSONAS,
    DEFAULT_PERSONA,
    get_persona_fragment,
    list_personas,
)
