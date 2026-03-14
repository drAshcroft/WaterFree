"""Runtime/skill/checkpoint/subagent management handlers."""
from __future__ import annotations

import os

from backend.knowledge.store import KnowledgeStore
from backend.llm.personas import list_personas, reload_personas, save_persona_documents
from backend.llm.provider_profiles import normalize_provider_profile
from backend.llm.runtime_registry import (
    list_runtime_descriptors,
    resolve_runtime_name,
)
from backend.llm.tools import build_default_tool_registry
from backend.todo.store import TaskStore


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


def handle_list_personas(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    runtime = server._get_runtime(workspace_path)
    registry = getattr(runtime, "_tool_registry", None)
    if registry is None:
        registry = build_default_tool_registry(
            graph=None,
            task_store_factory=lambda target_workspace: TaskStore(target_workspace),
            knowledge_store_factory=lambda: KnowledgeStore(),
            enable_optional_web_tools=bool(os.environ.get("WATERFREE_ENABLE_WEB_TOOLS", "").strip()),
        )
    personas = []
    for persona in list_personas():
        entry = dict(persona)
        entry["tools"] = registry.describe_persona_tools(
            persona=str(persona.get("id", "")),
            include_optional=False,
        )
        personas.append(entry)
    return {"personas": personas}


def handle_save_personas(server, params: dict) -> dict:
    _ = server
    personas = params.get("personas", [])
    saved = save_persona_documents(personas if isinstance(personas, list) else [])
    reload_personas()
    return {"ok": True, "personas": saved}


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


def handle_get_provider_capabilities(server, params: dict) -> dict:
    """Return active-provider capability metadata for UI/runtime consumers."""
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    profile_doc = server._get_provider_profile(workspace_path)
    active = next(
        (p for p in profile_doc.catalog if p.id == profile_doc.active_provider_id),
        profile_doc.catalog[0] if profile_doc.catalog else None,
    )
    if active is None:
        return {"capabilities": None}
    stages = ["planning", "annotation", "execution", "debug", "question_answer"]
    return {
        "capabilities": {
            "providerId": active.id,
            "providerType": active.type,
            "providerKind": active.provider_kind(),
            "label": active.label,
            "runtimeFamily": "anthropic" if active.type == "claude" else active.type,
            "models": {stage: active.model_for_stage(stage) for stage in stages},
            "features": {
                "tools": active.features.tools,
                "skills": active.features.skills,
                "checkpoints": active.features.checkpoints,
                "subagents": active.features.subagents,
                "summarization": active.features.summarization,
            },
            "cachePolicy": {
                "enablePromptCaching": active.optimizations.anthropic.get("enablePromptCaching", False)
                if active.type == "claude" else False,
                "sessionKeyStrategy": profile_doc.policies.session_key_strategy,
                "flushOnTaskComplete": profile_doc.policies.flush_on_task_complete,
                "flushOnProviderSwitch": profile_doc.policies.flush_on_provider_switch,
            },
            "summarizationThresholds": dict(profile_doc.policies.summarization_thresholds),
            "supportedStages": list(active.routing.use_for_stages),
            "activeProfileHash": profile_doc.profile_hash,
        }
    }


def handle_get_usage_stats(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    runtime = server._get_runtime(workspace_path)
    get_usage = getattr(runtime, "get_usage_stats", None)
    if not callable(get_usage):
        return {"providers": [], "byPersona": [], "byStage": []}
    result = get_usage(workspace_path)
    if isinstance(result, dict):
        return result
    # Legacy: runtime returned a flat list of providers
    return {"providers": result, "byPersona": [], "byStage": []}


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
