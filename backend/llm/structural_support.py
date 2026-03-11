"""
Shared helpers for structural/pattern-expert routing and rich task parsing.
"""

from __future__ import annotations

import re
from typing import Any

from backend.llm.personas import DEFAULT_PERSONA, PERSONAS
from backend.session.models import (
    CodeCoord,
    DependencyType,
    OwnerType,
    Task,
    TaskDependency,
    TaskOwner,
    TaskPriority,
    TaskType,
)

_STRONG_SIGNAL_PATTERNS = (
    "api client",
    "api integration",
    "external api",
    "third-party api",
    "event flow",
    "data flow",
    "refactor architecture",
    "service boundary",
    "ownership boundary",
)

_WEAK_SIGNAL_PATTERNS = (
    "anti-pattern",
    "api",
    "boundary",
    "boundaries",
    "contract",
    "contracts",
    "coupling",
    "data contract",
    "event",
    "failure mode",
    "failure modes",
    "integration",
    "integrations",
    "interface",
    "interfaces",
    "layer",
    "layers",
    "pattern",
    "patterns",
    "policy",
    "policies",
    "rate limit",
    "retry",
    "schema",
    "subsystem",
    "subsystems",
    "webhook",
)

_ROUTABLE_STAGES = {"planning", "annotation", "alter_annotation", "question_answer"}


def normalize_persona(persona: str) -> str:
    candidate = (persona or "").strip().lower()
    if candidate in PERSONAS:
        return candidate
    return DEFAULT_PERSONA


def route_structural_persona(persona: str, stage: str, *texts: str) -> str:
    resolved = normalize_persona(persona)
    if resolved == "pattern_expert":
        return resolved
    if resolved != "architect":
        return resolved
    if stage.strip().lower() not in _ROUTABLE_STAGES:
        return resolved
    return "pattern_expert" if should_delegate_to_pattern_expert(*texts) else resolved


def should_delegate_to_pattern_expert(*texts: str) -> bool:
    haystack = " ".join(text for text in texts if isinstance(text, str) and text.strip()).lower()
    if not haystack:
        return False
    if any(pattern in haystack for pattern in _STRONG_SIGNAL_PATTERNS):
        return True
    score = 0
    for pattern in _WEAK_SIGNAL_PATTERNS:
        if pattern in haystack:
            score += 1
    if re.search(r"\b(interface|api|schema|subsystem|integration)\b.*\b(interface|api|schema|subsystem|integration)\b", haystack):
        score += 1
    return score >= 2


def task_from_raw(raw: dict[str, Any]) -> Task:
    owner = raw.get("owner")
    if isinstance(owner, dict):
        owner_value = TaskOwner.from_dict(owner)
    else:
        owner_value = TaskOwner(
            type=_coerce_owner_type(raw.get("ownerType")),
            name=str(raw.get("ownerName", "")).strip(),
        )
    return Task(
        title=str(raw.get("title", "")).strip(),
        description=str(raw.get("description", "")).strip(),
        rationale=str(raw.get("rationale", "")).strip(),
        target_coord=CodeCoord(
            file=str(raw.get("targetFile", "")).strip(),
            method=str(raw.get("targetFunction", "")).strip() or None,
        ),
        context_coords=_coerce_context_coords(raw.get("contextCoords", [])),
        priority=_coerce_priority(raw.get("priority", "P2")),
        phase=str(raw.get("phase", "")).strip() or None,
        depends_on=[],
        owner=owner_value,
        task_type=_coerce_task_type(raw.get("taskType", "impl")),
        estimated_minutes=_coerce_int(raw.get("estimatedMinutes")),
        ai_notes=str(raw.get("aiNotes", "")).strip() or None,
    )


def apply_task_dependencies(tasks: list[Task], raw_tasks: list[dict[str, Any]]) -> None:
    ref_to_id: dict[str, str] = {}
    for raw, task in zip(raw_tasks, tasks):
        for ref in _task_refs(raw, task):
            ref_to_id[ref] = task.id

    for raw, task in zip(raw_tasks, tasks):
        task.depends_on = _coerce_dependencies(raw.get("dependsOn", []), ref_to_id)


def _task_refs(raw: dict[str, Any], task: Task) -> list[str]:
    refs = [task.id]
    title = str(raw.get("title", "")).strip()
    if title:
        refs.append(title.casefold())
    for key in ("id", "ref", "taskId", "todoId"):
        value = str(raw.get(key, "")).strip()
        if value:
            refs.append(value)
    return refs


def _coerce_dependencies(raw: Any, ref_to_id: dict[str, str]) -> list[TaskDependency]:
    deps: list[TaskDependency] = []
    if not isinstance(raw, list):
        return deps
    for item in raw:
        if isinstance(item, str):
            ref = item.strip()
            task_id = ref_to_id.get(ref) or ref_to_id.get(ref.casefold())
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
        task_id = ref_to_id.get(ref) or ref_to_id.get(ref.casefold()) or ref
        if not task_id:
            continue
        deps.append(
            TaskDependency(
                task_id=task_id,
                type=_coerce_dependency_type(item.get("type", "blocks")),
            )
        )
    return deps


def _coerce_context_coords(raw: Any) -> list[CodeCoord]:
    if not isinstance(raw, list):
        return []
    coords: list[CodeCoord] = []
    for item in raw:
        if isinstance(item, dict):
            coords.append(CodeCoord.from_dict(item))
    return coords


def _coerce_priority(raw_priority: Any) -> TaskPriority:
    if isinstance(raw_priority, str):
        try:
            return TaskPriority(raw_priority)
        except ValueError:
            return TaskPriority.P2
    if isinstance(raw_priority, int):
        return {
            0: TaskPriority.P0,
            1: TaskPriority.P1,
            2: TaskPriority.P2,
            3: TaskPriority.P3,
        }.get(raw_priority, TaskPriority.P2)
    return TaskPriority.P2


def _coerce_task_type(raw: Any) -> TaskType:
    try:
        return TaskType(str(raw))
    except Exception:
        return TaskType.IMPL


def _coerce_owner_type(raw: Any) -> OwnerType:
    try:
        return OwnerType(str(raw))
    except Exception:
        return OwnerType.UNASSIGNED


def _coerce_dependency_type(raw: Any) -> DependencyType:
    try:
        return DependencyType(str(raw))
    except Exception:
        return DependencyType.BLOCKS


def _coerce_int(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except Exception:
        return None
