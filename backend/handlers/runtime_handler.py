"""Runtime/skill/checkpoint/subagent management handlers."""
from __future__ import annotations

import os

from backend.llm.provider_profiles import normalize_provider_profile
from backend.llm.runtime_registry import (
    list_runtime_descriptors,
    resolve_runtime_name,
)


def handle_list_runtimes(server, params: dict) -> dict:
    _ = params
    return {"runtimes": [descriptor.to_dict() for descriptor in list_runtime_descriptors()]}


def handle_get_active_runtime(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    profile = server._get_provider_profile(workspace_path)
    return {"runtimeId": server._runtime_name, "providerId": profile.active_provider_id}


def handle_set_active_runtime(server, params: dict) -> dict:
    runtime_id = resolve_runtime_name(str(params.get("runtimeId", "")))
    if runtime_id == server._runtime_name:
        return {"ok": True, "runtimeId": server._runtime_name}
    server._runtime_name = runtime_id
    server._clear_runtime_cache()
    return {"ok": True, "runtimeId": server._runtime_name}


def handle_sync_provider_profile(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    profile = normalize_provider_profile(params.get("profile", {}))
    profile_hash = server._set_provider_profile(workspace_path, profile)
    return {"ok": True, "profileHash": profile_hash}


def handle_list_skills(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    persona = str(params.get("persona", ""))
    stage = str(params.get("stage", ""))
    runtime = server._get_runtime(workspace_path)
    list_skills = getattr(runtime, "list_skills", None)
    if callable(list_skills):
        return {"skills": list_skills(persona=persona, stage=stage)}
    from backend.llm.skills.registry import SkillRegistry
    registry = SkillRegistry(workspace_path)
    return {"skills": registry.to_dicts(persona=persona, stage=stage)}


def handle_reload_skills(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    runtime = server._get_runtime(workspace_path)
    refresh = getattr(runtime, "refresh_skills", None)
    if callable(refresh):
        count = int(refresh())
        return {"ok": True, "count": count}
    from backend.llm.skills.registry import SkillRegistry
    count = len(SkillRegistry(workspace_path).reload())
    return {"ok": True, "count": count}


def handle_get_skill_detail(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    skill_id = str(params.get("skillId", "")).strip()
    if not skill_id:
        raise ValueError("skillId is required")
    runtime = server._get_runtime(workspace_path)
    get_detail = getattr(runtime, "get_skill_detail", None)
    if callable(get_detail):
        return get_detail(skill_id)
    from backend.llm.skills.registry import SkillRegistry
    return SkillRegistry(workspace_path).get_skill_detail(skill_id)


def handle_list_checkpoints(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    session_id = str(params.get("sessionId", ""))
    runtime = server._get_runtime(workspace_path)
    list_checkpoints = getattr(runtime, "list_checkpoints", None)
    if not callable(list_checkpoints):
        return {"checkpoints": []}
    checkpoints = list_checkpoints(workspace_path, session_id=session_id)
    return {"checkpoints": checkpoints}


def handle_resume_checkpoint(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    checkpoint_id = str(params.get("checkpointId", "")).strip()
    if not checkpoint_id:
        raise ValueError("checkpointId is required")
    decision = dict(params.get("decision", {}))
    decision.setdefault("workspacePath", workspace_path)
    runtime = server._get_runtime(workspace_path)
    return {"ok": True, "checkpoint": runtime.resume(checkpoint_id, decision)}


def handle_discard_checkpoint(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    checkpoint_id = str(params.get("checkpointId", "")).strip()
    if not checkpoint_id:
        raise ValueError("checkpointId is required")
    runtime = server._get_runtime(workspace_path)
    discard = getattr(runtime, "discard", None)
    if not callable(discard):
        raise ValueError("Runtime does not support checkpoint discard.")
    result = discard(checkpoint_id, {"workspacePath": workspace_path})
    return {"ok": bool(result.get("ok", False)), "checkpointId": checkpoint_id}


def handle_list_subagents(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    runtime = server._get_runtime(workspace_path)
    list_subagents = getattr(runtime, "list_subagents", None)
    if not callable(list_subagents):
        return {"subagents": []}
    return {"subagents": list_subagents()}


def handle_get_usage_stats(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    runtime = server._get_runtime(workspace_path)
    get_usage = getattr(runtime, "get_usage_stats", None)
    if not callable(get_usage):
        return {"providers": []}
    return {"providers": get_usage(workspace_path)}


def handle_delegate_to_subagent(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    session_id = str(params.get("sessionId", "")).strip()
    subagent_id = str(params.get("subagentId", "")).strip()
    task_id = str(params.get("taskId", "")).strip()
    prompt = str(params.get("prompt", "")).strip()
    if not session_id or not subagent_id or not task_id:
        raise ValueError("sessionId, subagentId, and taskId are required")
    runtime = server._get_runtime(workspace_path)
    delegate = getattr(runtime, "delegate_to_subagent", None)
    if not callable(delegate):
        raise ValueError("Runtime does not support subagent delegation.")
    return delegate(
        session_id=session_id,
        subagent_id=subagent_id,
        task_id=task_id,
        prompt=prompt,
        workspace_path=workspace_path,
    )
