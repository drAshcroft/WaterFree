from __future__ import annotations

from backend.session.models import (
    CodeCoord,
    OwnerType,
    Task,
    TaskOwner,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from backend.wizard.definitions import CODING_TEMPLATE
from backend.wizard.models import (
    WizardChunkStatus,
    WizardRun,
    WizardStageState,
    WizardStageStatus,
    WizardTodoExport,
)


def _coerce_priority(raw: str) -> TaskPriority:
    try:
        return TaskPriority(raw)
    except Exception:
        return TaskPriority.P2


def _coerce_task_type(raw: str) -> TaskType:
    try:
        return TaskType(raw)
    except Exception:
        return TaskType.IMPL


def _coerce_owner_type(raw: str) -> OwnerType:
    try:
        return OwnerType(raw)
    except Exception:
        return OwnerType.UNASSIGNED


class TodoExporter:
    """Converts wizard runs/chunks to tasks and manages todo promotion."""

    def merge_todo_exports(
        self,
        stage: WizardStageState,
        raw_todos: list[dict],
    ) -> list[WizardTodoExport]:
        existing_by_key = {
            self.todo_identity(todo): todo
            for todo in stage.todo_exports
        }
        merged: list[WizardTodoExport] = []
        for raw in raw_todos:
            todo = WizardTodoExport(
                stage_id=stage.id,
                title=str(raw.get("title", "")).strip(),
                description=str(raw.get("description", "")).strip(),
                prompt=str(raw.get("prompt", "")).strip(),
                phase=str(raw.get("phase", "")).strip() or stage.title,
                priority=_coerce_priority(str(raw.get("priority", "P2"))),
                task_type=_coerce_task_type(str(raw.get("taskType", "impl"))),
                target_coord=CodeCoord(
                    file=str(raw.get("targetFile", "")).strip(),
                    method=str(raw.get("targetFunction", "")).strip() or None,
                ),
                owner_type=_coerce_owner_type(str(raw.get("ownerType", OwnerType.UNASSIGNED.value))),
                owner_name=str(raw.get("ownerName", "")).strip(),
            )
            if not todo.title:
                continue
            identity = self.todo_identity(todo)
            if identity in existing_by_key:
                todo.id = existing_by_key[identity].id
                todo.promoted_task_id = existing_by_key[identity].promoted_task_id
            merged.append(todo)
        return merged

    def todo_identity(self, todo: WizardTodoExport) -> str:
        return "|".join([
            todo.title.strip().lower(),
            todo.description.strip().lower(),
            todo.task_type.value,
            todo.target_coord.file.strip().lower(),
            (todo.target_coord.method or "").strip().lower(),
        ])

    def todo_to_task_input(self, stage: WizardStageState, todo: WizardTodoExport) -> dict:
        return {
            "title": todo.title,
            "description": todo.description or todo.prompt or todo.title,
            "phase": todo.phase or stage.title,
            "priority": todo.priority.value,
            "taskType": todo.task_type.value,
            "owner": {
                "type": todo.owner_type.value,
                "name": todo.owner_name,
            },
            "targetCoord": todo.target_coord.to_dict(),
        }

    def build_session_tasks(self, run: WizardRun) -> list[Task]:
        tasks: list[Task] = []
        for stage in run.stages:
            if stage.status != WizardStageStatus.ACCEPTED:
                continue
            for todo in stage.todo_exports:
                if todo.task_type == TaskType.SPIKE:
                    continue
                tasks.append(
                    Task(
                        title=todo.title,
                        description=todo.description or todo.prompt or todo.title,
                        priority=todo.priority,
                        phase=todo.phase or stage.title,
                        owner=TaskOwner(type=todo.owner_type, name=todo.owner_name),
                        task_type=todo.task_type,
                        target_coord=todo.target_coord,
                        status=TaskStatus.PENDING,
                    )
                )
        if tasks:
            return tasks
        return [
            Task(
                title=f"Implement {run.goal}",
                description="No todo exports were promoted, so coding starts from the accepted wizard summary.",
                priority=TaskPriority.P1,
                phase=CODING_TEMPLATE.title,
                owner=TaskOwner(type=OwnerType.UNASSIGNED, name=""),
                task_type=TaskType.IMPL,
                status=TaskStatus.PENDING,
            )
        ]
