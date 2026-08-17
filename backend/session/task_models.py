"""
Task domain models — Task, dependencies, ownership, priority, and status.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid

from backend.session.coord_models import CodeCoord
from backend.session.enum_coercion import coerce_enum, register_enum_aliases


# ── Task Priority ──────────────────────────────────────────────────────────────
# P0 = blocker, P1 = critical path, P2 = should do this session,
# P3 = backlog (deferred), spike = research/decision (no code produced)

class TaskPriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    SPIKE = "spike"


# Case folding is handled by the coercion helpers, so "p0" needs no entry here.
register_enum_aliases(TaskPriority, {
    "blocker": TaskPriority.P0,
    "critical": TaskPriority.P1,
    "backlog": TaskPriority.P3,
    "research": TaskPriority.SPIKE,
})


# ── Dependency ────────────────────────────────────────────────────────────────

class DependencyType(str, Enum):
    BLOCKS = "blocks"           # hard: cannot start until dependency completes
    INFORMS = "informs"         # soft: output changes how this task is done
    SHARES_FILE = "shares-file" # warns of conflict risk if worked in parallel


# Deliberately no "blocked-by" alias: it names the opposite edge direction, and
# silently reinterpreting it would reverse a dependency instead of fixing it.
register_enum_aliases(DependencyType, {
    "hard": DependencyType.BLOCKS,
    "soft": DependencyType.INFORMS,
    "shares_file": DependencyType.SHARES_FILE,
    "sharesfile": DependencyType.SHARES_FILE,
    "shared-file": DependencyType.SHARES_FILE,
})


@dataclass
class TaskDependency:
    task_id: str = ""
    type: DependencyType = DependencyType.BLOCKS

    def to_dict(self) -> dict:
        return {"taskId": self.task_id, "type": self.type.value}

    @classmethod
    def from_dict(cls, d: dict, warnings: Optional[list[str]] = None) -> TaskDependency:
        return cls(
            task_id=d.get("taskId", ""),
            type=coerce_enum(
                DependencyType, d.get("type"), DependencyType.BLOCKS,
                field="dependsOn[].type", warnings=warnings,
            ),
        )

    @classmethod
    def from_payload(cls, value: object, warnings: Optional[list[str]] = None) -> Optional[TaskDependency]:
        """Read one stored `dependsOn` entry of unknown shape. None means drop it.

        A bare string is how an outside writer spells a reference — usually a
        task key rather than an id, which nothing at this layer can resolve.
        Keep it as the id anyway: `TaskStore` repairs key-shaped references when
        it has every task in hand, and `validate` reports whatever is left.
        Dropping it silently would lose a real edge in the dependency graph.
        """
        if isinstance(value, dict):
            return cls.from_dict(value, warnings)
        if isinstance(value, str) and value.strip():
            if warnings is not None:
                warnings.append(f"dependsOn: bare reference {value!r} read as a task reference")
            return cls(task_id=value.strip())
        if warnings is not None:
            warnings.append(f"dependsOn: entry of unusable shape ({type(value).__name__}) dropped")
        return None


# ── Owner ─────────────────────────────────────────────────────────────────────

class OwnerType(str, Enum):
    HUMAN = "human"
    AGENT = "agent"
    UNASSIGNED = "unassigned"


register_enum_aliases(OwnerType, {
    "ai": OwnerType.AGENT,
    "assistant": OwnerType.AGENT,
    "bot": OwnerType.AGENT,
    "user": OwnerType.HUMAN,
    "person": OwnerType.HUMAN,
    "none": OwnerType.UNASSIGNED,
    "nobody": OwnerType.UNASSIGNED,
})


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
    def from_dict(cls, d: dict, warnings: Optional[list[str]] = None) -> TaskOwner:
        return cls(
            type=coerce_enum(
                OwnerType, d.get("type"), OwnerType.UNASSIGNED,
                field="owner.type", warnings=warnings,
            ),
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


register_enum_aliases(TaskType, {
    "implementation": TaskType.IMPL,
    "implement": TaskType.IMPL,
    "code": TaskType.IMPL,
    "tests": TaskType.TEST,
    "testing": TaskType.TEST,
    "bug": TaskType.BUG_FIX,
    "bugfix": TaskType.BUG_FIX,
    "bug-fix": TaskType.BUG_FIX,
    "fix": TaskType.BUG_FIX,
    "feat": TaskType.FEATURE,
    "chore": TaskType.TASK,
    "research": TaskType.SPIKE,
})


# ── Task Timing ───────────────────────────────────────────────────────────────

class TaskTiming(str, Enum):
    ONE_TIME = "one_time"
    RECURRING = "recurring"


register_enum_aliases(TaskTiming, {
    "one-time": TaskTiming.ONE_TIME,
    "onetime": TaskTiming.ONE_TIME,
    "once": TaskTiming.ONE_TIME,
    "single": TaskTiming.ONE_TIME,
    "repeating": TaskTiming.RECURRING,
    "repeat": TaskTiming.RECURRING,
})


# ── Task Status ───────────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING = "pending"
    ANNOTATING = "annotating"
    NEGOTIATING = "negotiating"
    EXECUTING = "executing"
    COMPLETE = "complete"
    SKIPPED = "skipped"


# The `completed` / `in_progress` / `todo` family is Claude Code's `TodoWrite`
# vocabulary, which agents reach for when they write into a store directly. It
# has cost one workspace its whole board already, so translate rather than fail.
# A blocked task is genuinely not started here — WaterFree carries that state in
# `blockedReason` and `dependsOn`, not in the status.
register_enum_aliases(TaskStatus, {
    "completed": TaskStatus.COMPLETE,
    "done": TaskStatus.COMPLETE,
    "finished": TaskStatus.COMPLETE,
    "closed": TaskStatus.COMPLETE,
    "in_progress": TaskStatus.EXECUTING,
    "in-progress": TaskStatus.EXECUTING,
    "inprogress": TaskStatus.EXECUTING,
    "active": TaskStatus.EXECUTING,
    "started": TaskStatus.EXECUTING,
    "working": TaskStatus.EXECUTING,
    "todo": TaskStatus.PENDING,
    "to-do": TaskStatus.PENDING,
    "open": TaskStatus.PENDING,
    "not_started": TaskStatus.PENDING,
    "not-started": TaskStatus.PENDING,
    "new": TaskStatus.PENDING,
    "blocked": TaskStatus.PENDING,
    "skip": TaskStatus.SKIPPED,
    "cancelled": TaskStatus.SKIPPED,
    "canceled": TaskStatus.SKIPPED,
    "wontfix": TaskStatus.SKIPPED,
    "deferred": TaskStatus.SKIPPED,
})


# ── Payload shape guards ──────────────────────────────────────────────────────
# Stored payloads can be the wrong *shape*, not just carry the wrong values —
# the same outside writers that invent enum spellings also write a bare string
# where a list of objects belongs. These keep `from_dict` reading rather than
# raising, and leave a trail of what was ignored.

def _payload_items(value: object, *, field: str, warnings: Optional[list[str]]) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if warnings is not None:
        warnings.append(f"{field}: expected a list, got {type(value).__name__} — ignored")
    return []


def _payload_dicts(value: object, *, field: str, warnings: Optional[list[str]]) -> list[dict]:
    items = []
    for item in _payload_items(value, field=field, warnings=warnings):
        if isinstance(item, dict):
            items.append(item)
        elif warnings is not None:
            warnings.append(f"{field}: entry of unusable shape ({type(item).__name__}) dropped")
    return items


def _payload_dict(value: object, *, field: str, warnings: Optional[list[str]]) -> Optional[dict]:
    if value is None or isinstance(value, dict):
        return value
    if warnings is not None:
        warnings.append(f"{field}: expected an object, got {type(value).__name__} — ignored")
    return None


# ── Task ──────────────────────────────────────────────────────────────────────

@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    key: str = ""
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

    # Not persisted, not compared: what `from_dict` had to repair to read this
    # task at all. `TaskStore.validate` reports these; the next save clears them
    # by writing the canonical spelling back.
    parse_warnings: list[str] = field(default_factory=list, compare=False, repr=False)

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
            "key": self.key,
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
        """Read a stored payload. Never raises on an unrecognized enum value.

        This runs inside `TaskStore.__init__`, so raising here would take a whole
        workspace's board and CLI down over one bad row. Unrecognized values are
        coerced to the field default and recorded in `parse_warnings` instead —
        see `backend.session.enum_coercion` for why that trade is the right way
        round.
        """
        from backend.session.annotation_models import IntentAnnotation  # avoid circular import
        warnings: list[str] = []
        target_coord = _payload_dict(d.get("targetCoord"), field="targetCoord", warnings=warnings)
        owner = _payload_dict(d.get("owner"), field="owner", warnings=warnings)
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            key=d.get("key", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            rationale=d.get("rationale", ""),
            target_coord=CodeCoord.from_dict(target_coord, warnings) if target_coord else CodeCoord(),
            context_coords=[
                CodeCoord.from_dict(c, warnings)
                for c in _payload_dicts(d.get("contextCoords"), field="contextCoords", warnings=warnings)
            ],
            priority=coerce_enum(
                TaskPriority, d.get("priority"), TaskPriority.P2,
                field="priority", warnings=warnings,
            ),
            phase=d.get("phase"),
            depends_on=[
                dep for dep in (
                    TaskDependency.from_payload(raw, warnings)
                    for raw in _payload_items(d.get("dependsOn"), field="dependsOn", warnings=warnings)
                ) if dep is not None
            ],
            blocked_reason=d.get("blockedReason"),
            owner=TaskOwner.from_dict(owner, warnings) if owner else TaskOwner(),
            task_type=coerce_enum(
                TaskType, d.get("taskType"), TaskType.IMPL,
                field="taskType", warnings=warnings,
            ),
            estimated_minutes=d.get("estimatedMinutes"),
            actual_minutes=d.get("actualMinutes"),
            status=coerce_enum(
                TaskStatus, d.get("status"), TaskStatus.PENDING,
                field="status", warnings=warnings,
            ),
            human_notes=d.get("humanNotes"),
            ai_notes=d.get("aiNotes"),
            annotations=[
                IntentAnnotation.from_dict(a, warnings)
                for a in _payload_dicts(d.get("annotations"), field="annotations", warnings=warnings)
            ],
            started_at=d.get("startedAt"),
            completed_at=d.get("completedAt"),
            acceptance_criteria=d.get("acceptanceCriteria"),
            trigger=d.get("trigger"),
            timing=coerce_enum(
                TaskTiming, d.get("timing"), TaskTiming.ONE_TIME,
                field="timing", warnings=warnings,
            ),
            parse_warnings=warnings,
        )
