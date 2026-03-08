"""
Shared utilities and data containers for the todo store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.session.models import Task, TaskPriority


@dataclass
class TaskStoreData:
    version: int = 1
    tasks: list[Task] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    velocity_log: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "tasks": [task.to_dict() for task in self.tasks],
            "phases": self.phases,
            "updatedAt": self.updated_at,
            "velocityLog": self.velocity_log,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "TaskStoreData":
        return cls(
            version=int(payload.get("version", 1)),
            tasks=[Task.from_dict(item) for item in payload.get("tasks", [])],
            phases=[str(phase) for phase in payload.get("phases", [])],
            updated_at=str(payload.get("updatedAt") or now()),
            velocity_log=list(payload.get("velocityLog", [])),
        )


def task_sort_key(task: Task) -> tuple[int, int, str, str]:
    return (
        priority_rank(task.priority),
        0 if task.started_at else 1,
        task.phase or "",
        task.title.casefold(),
    )


def priority_rank(priority: TaskPriority) -> int:
    order = {
        TaskPriority.P0: 0,
        TaskPriority.P1: 1,
        TaskPriority.P2: 2,
        TaskPriority.P3: 3,
        TaskPriority.SPIKE: 4,
    }
    return order.get(priority, 99)


def instruction_title(instruction: str) -> str:
    text = " ".join(instruction.split())
    if len(text) <= 80:
        return text
    return text[:77].rstrip() + "..."


def to_workspace_relative(workspace: Path, file_path: str) -> str:
    if not file_path:
        return ""
    candidate = Path(file_path)
    if not candidate.is_absolute():
        return file_path.replace("\\", "/")
    try:
        return str(candidate.resolve().relative_to(workspace)).replace("\\", "/")
    except ValueError:
        return str(candidate.resolve()).replace("\\", "/")


def json_loads(raw: Optional[str], default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
