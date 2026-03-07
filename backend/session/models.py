"""
Core data models for PairProtocol.
Mirrors the TypeScript interfaces defined in the docs/ subsystem specs.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid


# ── Code Coordinates ──────────────────────────────────────────────────────────

class CoordAnchorType(str, Enum):
    CREATE_AT = "create-at"
    MODIFY = "modify"
    DELETE = "delete"
    READ_ONLY_CONTEXT = "read-only-context"


@dataclass
class CodeCoord:
    """Precise pointer into source code. Symbol name takes priority over line
    so annotations stay anchored when lines shift due to edits above the target."""
    file: str = ""                          # relative workspace path
    class_name: Optional[str] = None        # class name (if applicable)
    method: Optional[str] = None            # method/function name
    line: Optional[int] = None              # hint only — symbol name used first
    anchor_type: CoordAnchorType = CoordAnchorType.MODIFY

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "class": self.class_name,
            "method": self.method,
            "line": self.line,
            "anchorType": self.anchor_type.value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CodeCoord:
        return cls(
            file=d.get("file", ""),
            class_name=d.get("class"),
            method=d.get("method"),
            line=d.get("line"),
            anchor_type=CoordAnchorType(d.get("anchorType", "modify")),
        )


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


class AnnotationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    ALTERED = "altered"
    REDIRECTED = "redirected"


class TaskStatus(str, Enum):
    PENDING = "pending"
    ANNOTATING = "annotating"
    NEGOTIATING = "negotiating"
    EXECUTING = "executing"
    COMPLETE = "complete"
    SKIPPED = "skipped"


class SessionStatus(str, Enum):
    PLANNING = "planning"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"


@dataclass
class IntentAnnotation:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    # Precise source anchor — replaces loose targetFile/targetLine/targetFunction.
    # Renderer resolves target_coord.method via the parsed index so the annotation
    # stays anchored even when lines shift due to edits above the target.
    target_coord: CodeCoord = field(default_factory=CodeCoord)
    context_coords: list[CodeCoord] = field(default_factory=list)
    summary: str = ""                         # collapsed view — 1 sentence
    detail: str = ""                          # expanded view — full explanation
    approach: str = ""                        # specific technical approach
    will_create: list[str] = field(default_factory=list)
    will_modify: list[str] = field(default_factory=list)
    will_delete: list[str] = field(default_factory=list)
    side_effect_warnings: list[str] = field(default_factory=list)
    assumptions_made: list[str] = field(default_factory=list)
    questions_before_proceeding: list[str] = field(default_factory=list)
    status: AnnotationStatus = AnnotationStatus.PENDING
    human_response: Optional[str] = None
    created_at: Optional[str] = None
    reviewed_at: Optional[str] = None

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
            "taskId": self.task_id,
            "targetCoord": self.target_coord.to_dict(),
            "contextCoords": [c.to_dict() for c in self.context_coords],
            "summary": self.summary,
            "detail": self.detail,
            "approach": self.approach,
            "willCreate": self.will_create,
            "willModify": self.will_modify,
            "willDelete": self.will_delete,
            "sideEffectWarnings": self.side_effect_warnings,
            "assumptionsMade": self.assumptions_made,
            "questionsBeforeProceeding": self.questions_before_proceeding,
            "status": self.status.value,
            "humanResponse": self.human_response,
            "createdAt": self.created_at,
            "reviewedAt": self.reviewed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> IntentAnnotation:
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            task_id=d.get("taskId", ""),
            target_coord=CodeCoord.from_dict(d["targetCoord"]) if "targetCoord" in d else CodeCoord(),
            context_coords=[CodeCoord.from_dict(c) for c in d.get("contextCoords", [])],
            summary=d.get("summary", ""),
            detail=d.get("detail", ""),
            approach=d.get("approach", ""),
            will_create=d.get("willCreate", []),
            will_modify=d.get("willModify", []),
            will_delete=d.get("willDelete", []),
            side_effect_warnings=d.get("sideEffectWarnings", []),
            assumptions_made=d.get("assumptionsMade", []),
            questions_before_proceeding=d.get("questionsBeforeProceeding", []),
            status=AnnotationStatus(d.get("status", "pending")),
            human_response=d.get("humanResponse"),
            created_at=d.get("createdAt"),
            reviewed_at=d.get("reviewedAt"),
        )


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
    annotations: list[IntentAnnotation] = field(default_factory=list)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

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
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
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
        )


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
