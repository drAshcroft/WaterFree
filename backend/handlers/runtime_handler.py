"""Runtime/skill/checkpoint/subagent management handlers."""
from __future__ import annotations

import os

from backend.knowledge.store import KnowledgeStore
from backend.llm.checkpoints.store import CheckpointStore
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


def handle_preview_auto_route(server, params: dict) -> dict:
    """Catalog preview only: does not send completions or consume paid tokens."""
    from backend.llm.adapters.openrouter_adapter import resolve_api_key
    from backend.llm.openrouter_catalog import is_auto_model, load_models, select_candidates
    from backend.llm.provider_profiles import ALL_PROVIDER_STAGES, READER_STAGES, DEFAULT_STAGE_MODELS
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    sentinel, stage = str(params.get("model", "")).strip(), str(params.get("stage", "")).strip()
    if not is_auto_model(sentinel) or stage not in ALL_PROVIDER_STAGES:
        raise ValueError("Choose an automatic model and a supported stage.")
    document = server._get_provider_profile(workspace_path)
    profile = next((p for p in document.catalog if p.id == params.get("providerId")), None)
    if profile and profile.type != "openrouter":
        raise ValueError("Automatic routing requires an OpenRouter provider.")
    key = resolve_api_key(params.get("apiKey") or (profile.connection.api_key if profile else ""))
    ids = select_candidates(load_models(workspace_path=workspace_path, api_key=key or "", force_refresh=True),
                            sentinel=sentinel, require_tools=stage not in READER_STAGES)
    fallback_env = "WATERFREE_QA_SUMMARY_MODEL" if stage in READER_STAGES else "WATERFREE_AGENT_FALLBACK_MODEL"
    local = os.environ.get(fallback_env, DEFAULT_STAGE_MODELS["ollama"]["default"])
    return {"model": sentinel, "stage": stage,
            "chain": [{"provider": "openrouter", "model": model} for model in ids]
                     + [{"provider": "ollama", "model": local}],
            "note": "Catalog preview; endpoint and local-model availability are checked when a request runs."}


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


def handle_record_acp_delegation(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    if not os.path.isdir(workspace_path):
        raise ValueError("workspacePath must name an existing directory.")
    store = CheckpointStore(workspace_path)
    try:
        state = params.get("state")
        if state == "started":
            agent_id = str(params.get("agentId", "")).strip()
            prompt = str(params.get("prompt", "")).strip()
            if not agent_id or not prompt:
                raise ValueError("agentId and prompt are required.")
            checkpoint = store.create_checkpoint(
                session_id=str(params.get("taskId", "")), reason="ACP delegation audit",
                runtime_id="acp", subagent_id=agent_id, requires_approval=False,
                summary=f"Delegation to {agent_id}", status="running",
                payload={"mode": "audit", "agentId": agent_id, "prompt": prompt,
                         "model": params.get("model", ""), "workspacePath": workspace_path},
            )
        elif state == "finished":
            result = params.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise ValueError("result must include a boolean ok field.")
            checkpoint = store.finish_delegation(str(params.get("checkpointId", "")), result)
        else:
            raise ValueError("state must be started or finished.")
        return {"checkpoint": checkpoint}
    finally:
        store.close()


def handle_list_checkpoints(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    session_id = str(params.get("sessionId", ""))
    runtime = server._get_runtime(workspace_path)
    list_checkpoints = getattr(runtime, "list_checkpoints", None)
    checkpoints = list_checkpoints(workspace_path, session_id=session_id) if callable(list_checkpoints) else []
    store = CheckpointStore(workspace_path)
    try:
        known = {checkpoint["id"] for checkpoint in checkpoints}
        checkpoints.extend(checkpoint for checkpoint in store.list_checkpoints(session_id)
                           if checkpoint["runtimeId"] == "acp" and checkpoint["id"] not in known)
    finally:
        store.close()
    return {"checkpoints": checkpoints}


def handle_resume_checkpoint(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    checkpoint_id = str(params.get("checkpointId", "")).strip()
    if not checkpoint_id:
        raise ValueError("checkpointId is required")
    decision = dict(params.get("decision", {}))
    decision.setdefault("workspacePath", workspace_path)
    store = CheckpointStore(workspace_path)
    try:
        checkpoint = store.get_checkpoint(checkpoint_id)
        if checkpoint and not checkpoint["resumable"]:
            raise ValueError("ACP audit records cannot be resumed; start a new delegation explicitly.")
    finally:
        store.close()
    runtime = server._get_runtime(workspace_path)
    return {"ok": True, "checkpoint": runtime.resume(checkpoint_id, decision)}


def handle_discard_checkpoint(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    checkpoint_id = str(params.get("checkpointId", "")).strip()
    if not checkpoint_id:
        raise ValueError("checkpointId is required")
    store = CheckpointStore(workspace_path)
    try:
        checkpoint = store.get_checkpoint(checkpoint_id)
        if checkpoint and checkpoint["runtimeId"] == "acp":
            return {"ok": store.discard_checkpoint(checkpoint_id), "checkpointId": checkpoint_id}
    finally:
        store.close()
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


