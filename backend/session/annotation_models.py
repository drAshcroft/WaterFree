"""
Intent annotation models — AI-generated change proposals pending human review.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import uuid

from backend.session.coord_models import CodeCoord


class AnnotationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    ALTERED = "altered"
    REDIRECTED = "redirected"


@dataclass
class IntentAnnotation:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    # Precise source anchor — replaces loose targetFile/targetLine/targetFunction.
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
