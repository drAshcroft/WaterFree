from __future__ import annotations

from typing import Any

DESIGN_ARTIFACT_KEYS = (
    "subsystems",
    "interfaces",
    "interfaceMethods",
    "dataContracts",
    "apiCatalog",
    "patternChoices",
    "antiPatterns",
    "integrationPolicies",
    "todos",
)


def normalize_design_artifacts(
    payload: dict[str, Any] | None,
    *,
    fallback_subsystems: list[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    source = dict(payload or {})
    raw = source.get("designArtifacts")
    if not isinstance(raw, dict):
        raw = {}

    normalized = {key: _normalize_entries(raw.get(key, [])) for key in DESIGN_ARTIFACT_KEYS}
    if not normalized["subsystems"] and fallback_subsystems:
        normalized["subsystems"] = [
            {"name": name.strip()}
            for name in fallback_subsystems
            if isinstance(name, str) and name.strip()
        ]
    return normalized


def artifact_subsystem_names(design_artifacts: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in _normalize_entries(design_artifacts.get("subsystems", [])):
        name = str(item.get("name", "")).strip()
        if name:
            names.append(name)
    return names


def serialize_todo_summaries(todos: list[Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for todo in todos:
        title = str(getattr(todo, "title", "")).strip()
        if not title:
            continue
        summaries.append(
            {
                "id": str(getattr(todo, "id", "")).strip(),
                "title": title,
                "type": getattr(getattr(todo, "task_type", None), "value", ""),
                "priority": getattr(getattr(todo, "priority", None), "value", ""),
                "targetFile": str(getattr(getattr(todo, "target_coord", None), "file", "")).strip(),
            }
        )
    return summaries


def _normalize_entries(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            cleaned = {
                str(key): value
                for key, value in item.items()
                if str(key).strip() and value not in (None, "", [], {})
            }
            if cleaned:
                entries.append(cleaned)
            continue
        if isinstance(item, str) and item.strip():
            entries.append({"name": item.strip()})
    return entries
