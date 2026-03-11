"""
Core data models for WaterFree.

This module re-exports all domain models for backward compatibility.
Import from the sub-modules directly for clarity:
  - coord_models:      CoordAnchorType, CodeCoord
  - task_models:       TaskPriority, DependencyType, TaskDependency,
                       OwnerType, TaskOwner, TaskType, TaskStatus, Task
  - annotation_models: AnnotationStatus, IntentAnnotation
  - models (here):     AIState, SessionStatus, SessionNote, PlanDocument
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid

# Re-exports — keep callers working without changes
from backend.session.coord_models import CoordAnchorType, CodeCoord
from backend.session.task_models import (
    TaskPriority, DependencyType, TaskDependency,
    OwnerType, TaskOwner, TaskType, TaskTiming, TaskStatus, Task,
)
from backend.session.annotation_models import AnnotationStatus, IntentAnnotation

__all__ = [
    # coord
    "CoordAnchorType", "CodeCoord",
    # task
    "TaskPriority", "DependencyType", "TaskDependency",
    "OwnerType", "TaskOwner", "TaskType", "TaskTiming", "TaskStatus", "Task",
    # annotation
    "AnnotationStatus", "IntentAnnotation",
    # session
    "AIState", "SessionStatus", "SessionNote", "PlanDocument",
]


# ── AI / Session State ────────────────────────────────────────────────────────

class AIState(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    ANNOTATING = "annotating"
    AWAITING_REVIEW = "awaiting_review"
    EXECUTING = "executing"
    SCANNING = "scanning"
    ANSWERING = "answering"
    AWAITING_REDIRECT = "awaiting_redirect"


class SessionStatus(str, Enum):
    PLANNING = "planning"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"


@dataclass
class SessionNote:
    timestamp: str = ""
    author: str = ""  # "human" or "ai"
    text: str = ""

    def to_dict(self) -> dict:
        return {"timestamp": self.timestamp, "author": self.author, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict) -> SessionNote:
        return cls(
            timestamp=d.get("timestamp", ""),
            author=d.get("author", ""),
            text=d.get("text", ""),
        )


@dataclass
class PlanDocument:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    goal_statement: str = ""
    workspace_path: str = ""
    persona: str = "default"
    tasks: list[Task] = field(default_factory=list)
    status: SessionStatus = SessionStatus.PLANNING
    notes: list[SessionNote] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    ai_state: AIState = AIState.IDLE

    def current_task(self) -> Optional[Task]:
        for task in self.tasks:
            if task.status in (TaskStatus.PENDING, TaskStatus.ANNOTATING, TaskStatus.NEGOTIATING):
                return task
        return None

    def completed_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.status == TaskStatus.COMPLETE]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goalStatement": self.goal_statement,
            "workspacePath": self.workspace_path,
            "persona": self.persona,
            "tasks": [t.to_dict() for t in self.tasks],
            "status": self.status.value,
            "notes": [n.to_dict() for n in self.notes],
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "aiState": self.ai_state.value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> PlanDocument:
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            goal_statement=d.get("goalStatement", ""),
            workspace_path=d.get("workspacePath", ""),
            persona=d.get("persona", "default"),
            tasks=[Task.from_dict(t) for t in d.get("tasks", [])],
            status=SessionStatus(d.get("status", "planning")),
            notes=[SessionNote.from_dict(n) for n in d.get("notes", [])],
            created_at=d.get("createdAt", ""),
            updated_at=d.get("updatedAt", ""),
            ai_state=AIState(d.get("aiState", "idle")),
        )
