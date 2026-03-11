"""
Task domain models — Task, dependencies, ownership, priority, and status.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid

from backend.session.coord_models import CodeCoord


# ── Task Priority ──────────────────────────────────────────────────────────────
# P0 = blocker, P1 = critical path, P2 = should do this session,
# P3 = backlog (deferred), spike = research/decision (no code produced)

class TaskPriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    SPIKE = "spike"


# ── Dependency ────────────────────────────────────────────────────────────────

class DependencyType(str, Enum):
    BLOCKS = "blocks"           # hard: cannot start until dependency completes
    INFORMS = "informs"         # soft: output changes how this task is done
    SHARES_FILE = "shares-file" # warns of conflict risk if worked in parallel


@dataclass
class TaskDependency:
    task_id: str = ""
    type: DependencyType = DependencyType.BLOCKS

    def to_dict(self) -> dict:
        return {"taskId": self.task_id, "type": self.type.value}

    @classmethod
    def from_dict(cls, d: dict) -> TaskDependency:
        return cls(
            task_id=d.get("taskId", ""),
            type=DependencyType(d.get("type", "blocks")),
        )


# ── Owner ─────────────────────────────────────────────────────────────────────

class OwnerType(str, Enum):
    HUMAN = "human"
    AGENT = "agent"
    UNASSIGNED = "unassigned"


@dataclass
class TaskOwner:
    type: OwnerType = OwnerType.UNASSIGNED
    name: str = ""
    assigned_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "name": self.name,
            "assignedAt": self.assigned_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TaskOwner:
        return cls(
            type=OwnerType(d.get("type", "unassigned")),
            name=d.get("name", ""),
            assigned_at=d.get("assignedAt"),
        )


# ── Task Type ─────────────────────────────────────────────────────────────────

class TaskType(str, Enum):
    IMPL = "impl"
    TEST = "test"
    SPIKE = "spike"
    REVIEW = "review"
    REFACTOR = "refactor"
    PROTOCOL = "protocol"
    BUG_FIX = "bug_fix"
    FEATURE = "feature"
    TASK = "task"


# ── Task Timing ───────────────────────────────────────────────────────────────

class TaskTiming(str, Enum):
    ONE_TIME = "one_time"
    RECURRING = "recurring"


# ── Task Status ───────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING = "pending"
    ANNOTATING = "annotating"
    NEGOTIATING = "negotiating"
    EXECUTING = "executing"
    COMPLETE = "complete"
    SKIPPED = "skipped"


# ── Task ──────────────────────────────────────────────────────────────────────

@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    description: str = ""
    rationale: str = ""

    # Code location — where the work happens
    target_coord: CodeCoord = field(default_factory=CodeCoord)
    context_coords: list[CodeCoord] = field(default_factory=list)

    # Scheduling
    priority: TaskPriority = TaskPriority.P2
    phase: Optional[str] = None
    depends_on: list[TaskDependency] = field(default_factory=list)
    blocked_reason: Optional[str] = None

    # Ownership
    owner: TaskOwner = field(default_factory=TaskOwner)
    task_type: TaskType = TaskType.IMPL

    # Effort tracking
    estimated_minutes: Optional[int] = None
    actual_minutes: Optional[int] = None

    # Lifecycle
    status: TaskStatus = TaskStatus.PENDING
    human_notes: Optional[str] = None
    ai_notes: Optional[str] = None
    annotations: list = field(default_factory=list)  # list[IntentAnnotation] — avoid circular import
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    # Extended classification
    acceptance_criteria: Optional[str] = None
    trigger: Optional[str] = None
    timing: TaskTiming = TaskTiming.ONE_TIME

    @property
    def target_file(self) -> str:
        return self.target_coord.file

    @target_file.setter
    def target_file(self, value: str) -> None:
        self.target_coord.file = value

    @property
    def target_class(self) -> Optional[str]:
        return self.target_coord.class_name

    @target_class.setter
    def target_class(self, value: Optional[str]) -> None:
        self.target_coord.class_name = value

    @property
    def target_line(self) -> Optional[int]:
        return self.target_coord.line

    @target_line.setter
    def target_line(self, value: Optional[int]) -> None:
        self.target_coord.line = value

    @property
    def target_function(self) -> Optional[str]:
        return self.target_coord.method

    @target_function.setter
    def target_function(self, value: Optional[str]) -> None:
        self.target_coord.method = value

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "rationale": self.rationale,
            "targetCoord": self.target_coord.to_dict(),
            "contextCoords": [c.to_dict() for c in self.context_coords],
            "priority": self.priority.value,
            "phase": self.phase,
            "dependsOn": [d.to_dict() for d in self.depends_on],
            "blockedReason": self.blocked_reason,
            "owner": self.owner.to_dict(),
            "taskType": self.task_type.value,
            "estimatedMinutes": self.estimated_minutes,
            "actualMinutes": self.actual_minutes,
            "status": self.status.value,
            "humanNotes": self.human_notes,
            "aiNotes": self.ai_notes,
            "annotations": [a.to_dict() for a in self.annotations],
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "acceptanceCriteria": self.acceptance_criteria,
            "trigger": self.trigger,
            "timing": self.timing.value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        from backend.session.annotation_models import IntentAnnotation  # avoid circular import
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            title=d.get("title", ""),
            description=d.get("description", ""),
            rationale=d.get("rationale", ""),
            target_coord=CodeCoord.from_dict(d["targetCoord"]) if "targetCoord" in d else CodeCoord(),
            context_coords=[CodeCoord.from_dict(c) for c in d.get("contextCoords", [])],
            priority=TaskPriority(d.get("priority", "P2")),
            phase=d.get("phase"),
            depends_on=[TaskDependency.from_dict(dep) for dep in d.get("dependsOn", [])],
            blocked_reason=d.get("blockedReason"),
            owner=TaskOwner.from_dict(d["owner"]) if "owner" in d else TaskOwner(),
            task_type=TaskType(d.get("taskType", "impl")),
            estimated_minutes=d.get("estimatedMinutes"),
            actual_minutes=d.get("actualMinutes"),
            status=TaskStatus(d.get("status", "pending")),
            human_notes=d.get("humanNotes"),
            ai_notes=d.get("aiNotes"),
            annotations=[IntentAnnotation.from_dict(a) for a in d.get("annotations", [])],
            started_at=d.get("startedAt"),
            completed_at=d.get("completedAt"),
            acceptance_criteria=d.get("acceptanceCriteria"),
            trigger=d.get("trigger"),
            timing=TaskTiming(d.get("timing", TaskTiming.ONE_TIME.value)),
        )
