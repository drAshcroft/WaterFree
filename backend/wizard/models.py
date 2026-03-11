from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid

from backend.session.models import (
    CodeCoord,
    OwnerType,
    TaskDependency,
    TaskPriority,
    TaskType,
)


class WizardChunkStatus(str, Enum):
    DRAFT = "draft"
    ACCEPTED = "accepted"


class WizardStageStatus(str, Enum):
    PENDING = "pending"
    DRAFTED = "drafted"
    ACCEPTED = "accepted"


class WizardRunStatus(str, Enum):
    ACTIVE = "active"
    CODING = "coding"
    COMPLETE = "complete"


@dataclass
class WizardChunkState:
    id: str
    title: str
    required: bool = True
    visible: bool = True
    guidance: str = ""
    notes_snapshot: str = ""
    draft_text: str = ""
    accepted_text: str = ""
    status: WizardChunkStatus = WizardChunkStatus.DRAFT
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "required": self.required,
            "visible": self.visible,
            "guidance": self.guidance,
            "notesSnapshot": self.notes_snapshot,
            "draftText": self.draft_text,
            "acceptedText": self.accepted_text,
            "status": self.status.value,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WizardChunkState":
        return cls(
            id=str(payload.get("id", "")),
            title=str(payload.get("title", "")),
            required=bool(payload.get("required", True)),
            visible=bool(payload.get("visible", True)),
            guidance=str(payload.get("guidance", "")),
            notes_snapshot=str(payload.get("notesSnapshot", "")),
            draft_text=str(payload.get("draftText", "")),
            accepted_text=str(payload.get("acceptedText", "")),
            status=WizardChunkStatus(str(payload.get("status", WizardChunkStatus.DRAFT.value))),
            updated_at=str(payload.get("updatedAt", "")),
        )


@dataclass
class WizardTodoExport:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    stage_id: str = ""
    title: str = ""
    description: str = ""
    prompt: str = ""
    phase: str = ""
    priority: TaskPriority = TaskPriority.P2
    task_type: TaskType = TaskType.IMPL
    rationale: str = ""
    target_coord: CodeCoord = field(default_factory=CodeCoord)
    context_coords: list[CodeCoord] = field(default_factory=list)
    depends_on: list[TaskDependency] = field(default_factory=list)
    owner_type: OwnerType = OwnerType.UNASSIGNED
    owner_name: str = ""
    estimated_minutes: Optional[int] = None
    ai_notes: str = ""
    promoted_task_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "stageId": self.stage_id,
            "title": self.title,
            "description": self.description,
            "prompt": self.prompt,
            "phase": self.phase,
            "priority": self.priority.value,
            "taskType": self.task_type.value,
            "rationale": self.rationale,
            "targetCoord": self.target_coord.to_dict(),
            "contextCoords": [coord.to_dict() for coord in self.context_coords],
            "dependsOn": [dep.to_dict() for dep in self.depends_on],
            "ownerType": self.owner_type.value,
            "ownerName": self.owner_name,
            "estimatedMinutes": self.estimated_minutes,
            "aiNotes": self.ai_notes,
            "promotedTaskId": self.promoted_task_id,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WizardTodoExport":
        return cls(
            id=str(payload.get("id", str(uuid.uuid4()))),
            stage_id=str(payload.get("stageId", "")),
            title=str(payload.get("title", "")),
            description=str(payload.get("description", "")),
            prompt=str(payload.get("prompt", "")),
            phase=str(payload.get("phase", "")),
            priority=TaskPriority(str(payload.get("priority", TaskPriority.P2.value))),
            task_type=TaskType(str(payload.get("taskType", TaskType.IMPL.value))),
            rationale=str(payload.get("rationale", "")),
            target_coord=CodeCoord.from_dict(payload.get("targetCoord", {})),
            context_coords=[CodeCoord.from_dict(item) for item in payload.get("contextCoords", [])],
            depends_on=[TaskDependency.from_dict(item) for item in payload.get("dependsOn", [])],
            owner_type=OwnerType(str(payload.get("ownerType", OwnerType.UNASSIGNED.value))),
            owner_name=str(payload.get("ownerName", "")),
            estimated_minutes=payload.get("estimatedMinutes"),
            ai_notes=str(payload.get("aiNotes", "")),
            promoted_task_id=payload.get("promotedTaskId"),
        )


@dataclass
class WizardStageState:
    id: str
    kind: str
    title: str
    persona: str
    doc_path: str
    status: WizardStageStatus = WizardStageStatus.PENDING
    subsystem_name: str = ""
    chunks: list[WizardChunkState] = field(default_factory=list)
    todo_exports: list[WizardTodoExport] = field(default_factory=list)
    summary: str = ""
    questions: list[str] = field(default_factory=list)
    external_research_prompt: str = ""
    derived_artifacts: dict = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "persona": self.persona,
            "docPath": self.doc_path,
            "status": self.status.value,
            "subsystemName": self.subsystem_name,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "todoExports": [todo.to_dict() for todo in self.todo_exports],
            "summary": self.summary,
            "questions": list(self.questions),
            "externalResearchPrompt": self.external_research_prompt,
            "derivedArtifacts": dict(self.derived_artifacts),
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WizardStageState":
        return cls(
            id=str(payload.get("id", "")),
            kind=str(payload.get("kind", "")),
            title=str(payload.get("title", "")),
            persona=str(payload.get("persona", "architect")),
            doc_path=str(payload.get("docPath", "")),
            status=WizardStageStatus(str(payload.get("status", WizardStageStatus.PENDING.value))),
            subsystem_name=str(payload.get("subsystemName", "")),
            chunks=[WizardChunkState.from_dict(item) for item in payload.get("chunks", [])],
            todo_exports=[WizardTodoExport.from_dict(item) for item in payload.get("todoExports", [])],
            summary=str(payload.get("summary", "")),
            questions=[str(item) for item in payload.get("questions", [])],
            external_research_prompt=str(payload.get("externalResearchPrompt", "")),
            derived_artifacts=dict(payload.get("derivedArtifacts", {})),
            updated_at=str(payload.get("updatedAt", "")),
        )

    def get_chunk(self, chunk_id: str) -> Optional[WizardChunkState]:
        for chunk in self.chunks:
            if chunk.id == chunk_id:
                return chunk
        return None


@dataclass
class WizardRun:
    id: str
    wizard_id: str
    goal: str
    persona: str
    workspace_path: str
    status: WizardRunStatus = WizardRunStatus.ACTIVE
    current_stage_id: str = ""
    stages: list[WizardStageState] = field(default_factory=list)
    derived_task_ids: dict[str, str] = field(default_factory=dict)
    linked_session_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "wizardId": self.wizard_id,
            "goal": self.goal,
            "persona": self.persona,
            "workspacePath": self.workspace_path,
            "status": self.status.value,
            "currentStageId": self.current_stage_id,
            "stages": [stage.to_dict() for stage in self.stages],
            "derivedTaskIds": dict(self.derived_task_ids),
            "linkedSessionId": self.linked_session_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WizardRun":
        return cls(
            id=str(payload.get("id", "")),
            wizard_id=str(payload.get("wizardId", "")),
            goal=str(payload.get("goal", "")),
            persona=str(payload.get("persona", "architect")),
            workspace_path=str(payload.get("workspacePath", "")),
            status=WizardRunStatus(str(payload.get("status", WizardRunStatus.ACTIVE.value))),
            current_stage_id=str(payload.get("currentStageId", "")),
            stages=[WizardStageState.from_dict(item) for item in payload.get("stages", [])],
            derived_task_ids={str(key): str(value) for key, value in dict(payload.get("derivedTaskIds", {})).items()},
            linked_session_id=payload.get("linkedSessionId"),
            created_at=str(payload.get("createdAt", "")),
            updated_at=str(payload.get("updatedAt", "")),
        )

    def get_stage(self, stage_id: str) -> Optional[WizardStageState]:
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        return None
