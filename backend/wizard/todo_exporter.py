from __future__ import annotations

from backend.session.models import (
    CodeCoord,
    DependencyType,
    OwnerType,
    Task,
    TaskDependency,
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
        kept_raws: list[dict] = []
        for raw in raw_todos:
            todo = WizardTodoExport(
                stage_id=stage.id,
                title=str(raw.get("title", "")).strip(),
                description=str(raw.get("description", "")).strip(),
                prompt=str(raw.get("prompt", "")).strip(),
                phase=str(raw.get("phase", "")).strip() or stage.title,
                priority=_coerce_priority(str(raw.get("priority", "P2"))),
                task_type=_coerce_task_type(str(raw.get("taskType", "impl"))),
                rationale=str(raw.get("rationale", "")).strip(),
                target_coord=CodeCoord(
                    file=str(raw.get("targetFile", "")).strip(),
                    method=str(raw.get("targetFunction", "")).strip() or None,
                ),
                context_coords=[
                    CodeCoord.from_dict(item)
                    for item in raw.get("contextCoords", [])
                    if isinstance(item, dict)
                ],
                depends_on=[],
                owner_type=_coerce_owner_type(str(raw.get("ownerType", OwnerType.UNASSIGNED.value))),
                owner_name=str(raw.get("ownerName", "")).strip(),
                estimated_minutes=_coerce_int(raw.get("estimatedMinutes")),
                ai_notes=str(raw.get("aiNotes", "")).strip(),
            )
            if not todo.title:
                continue
            identity = self.todo_identity(todo)
            if identity in existing_by_key:
                todo.id = existing_by_key[identity].id
                todo.promoted_task_id = existing_by_key[identity].promoted_task_id
            merged.append(todo)
            kept_raws.append(raw)
        self._resolve_dependencies(kept_raws, merged)
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
            "rationale": todo.rationale,
            "phase": todo.phase or stage.title,
            "priority": todo.priority.value,
            "taskType": todo.task_type.value,
            "owner": {
                "type": todo.owner_type.value,
                "name": todo.owner_name,
            },
            "targetCoord": todo.target_coord.to_dict(),
            "contextCoords": [coord.to_dict() for coord in todo.context_coords],
            "dependsOn": [dep.to_dict() for dep in todo.depends_on],
            "estimatedMinutes": todo.estimated_minutes,
            "aiNotes": todo.ai_notes,
        }

    def build_session_tasks(self, run: WizardRun) -> list[Task]:
        tasks: list[Task] = []
        todo_to_task: dict[str, Task] = {}
        for stage in run.stages:
            if stage.status != WizardStageStatus.ACCEPTED:
                continue
            for todo in stage.todo_exports:
                if todo.task_type == TaskType.SPIKE:
                    continue
                task = Task(
                    title=todo.title,
                    description=todo.description or todo.prompt or todo.title,
                    rationale=todo.rationale,
                    priority=todo.priority,
                    phase=todo.phase or stage.title,
                    depends_on=[],
                    owner=TaskOwner(type=todo.owner_type, name=todo.owner_name),
                    task_type=todo.task_type,
                    target_coord=todo.target_coord,
                    context_coords=list(todo.context_coords),
                    estimated_minutes=todo.estimated_minutes,
                    ai_notes=todo.ai_notes or None,
                    status=TaskStatus.PENDING,
                )
                tasks.append(task)
                todo_to_task[todo.id] = task
        for stage in run.stages:
            if stage.status != WizardStageStatus.ACCEPTED:
                continue
            for todo in stage.todo_exports:
                task = todo_to_task.get(todo.id)
                if not task:
                    continue
                task.depends_on = [
                    TaskDependency(task_id=todo_to_task[dep.task_id].id, type=dep.type)
                    for dep in todo.depends_on
                    if dep.task_id in todo_to_task
                ]
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

    def _resolve_dependencies(
        self,
        raw_todos: list[dict],
        merged: list[WizardTodoExport],
    ) -> None:
        ref_to_id: dict[str, str] = {}
        for raw, todo in zip(raw_todos, merged):
            refs = {
                todo.id,
                todo.title.strip().casefold(),
                str(raw.get("id", "")).strip(),
                str(raw.get("ref", "")).strip(),
                str(raw.get("todoId", "")).strip(),
            }
            for ref in refs:
                if ref:
                    ref_to_id[ref] = todo.id

        for raw, todo in zip(raw_todos, merged):
            todo.depends_on = _coerce_dependencies(raw.get("dependsOn", []), ref_to_id)


def _coerce_dependencies(raw: object, ref_to_id: dict[str, str]) -> list[TaskDependency]:
    if not isinstance(raw, list):
        return []
    deps: list[TaskDependency] = []
    for item in raw:
        if isinstance(item, str):
            task_id = ref_to_id.get(item.strip()) or ref_to_id.get(item.strip().casefold())
            if task_id:
                deps.append(TaskDependency(task_id=task_id, type=DependencyType.BLOCKS))
            continue
        if not isinstance(item, dict):
            continue
        ref = (
            str(item.get("taskId", "")).strip()
            or str(item.get("todoId", "")).strip()
            or str(item.get("ref", "")).strip()
            or str(item.get("title", "")).strip()
        )
        task_id = ref_to_id.get(ref) or ref_to_id.get(ref.casefold())
        if not task_id:
            continue
        deps.append(
            TaskDependency(
                task_id=task_id,
                type=_coerce_dependency_type(item.get("type", "blocks")),
            )
        )
    return deps


def _coerce_dependency_type(raw: object) -> DependencyType:
    try:
        return DependencyType(str(raw))
    except Exception:
        return DependencyType.BLOCKS


def _coerce_int(raw: object) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except Exception:
        return None
