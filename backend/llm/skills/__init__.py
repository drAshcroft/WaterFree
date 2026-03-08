"""
Skill discovery and runtime adaptation.
"""

from .adapters import SkillAdapter, SkillBundle
from .loader import SkillInfo, discover_skills
from .registry import SkillRegistry

__all__ = [
    "SkillAdapter",
    "SkillBundle",
    "SkillInfo",
    "SkillRegistry",
    "discover_skills",
]
