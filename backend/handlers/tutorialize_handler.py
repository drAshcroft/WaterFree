"""
Tutorialize RPC handlers.

Three-phase conversational flow:

  1. analyzeTutorializeRepo  — Phase 1+2: scan repo, return key areas for selection.
  2. generateTutorials       — Phase 3+4: generate & store tutorials for chosen areas.
  3. tutorializeChat         — Follow-up Q&A using runtime.answer_question + knowledge search.
"""
from __future__ import annotations

import os
import uuid
import logging

log = logging.getLogger(__name__)


def _get_knowledge_store(server):
    if server._knowledge_store is None:
        from backend.knowledge.store import KnowledgeStore
        server._knowledge_store = KnowledgeStore()
    return server._knowledge_store


def handle_analyze_tutorialize_repo(server, params: dict) -> dict:
    """
    Phase 1 + 2: scan the repo and filter areas by the user's stated focus.

    Params:
        repoPath      — path to the repo to tutorialize (any local path)
        workspacePath — current VS Code workspace (fallback if repoPath omitted)
        focus         — what the user wants to learn (optional)

    Returns:
        sessionId  — opaque ID for subsequent generateTutorials / tutorializeChat calls
        overview   — 2-3 sentence project description
        areas      — list of {name, description, relevant_paths}
    """
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    # repoPath lets callers target any repo, not just the workspace
    repo_path_raw = str(params.get("repoPath", "")).strip()
    repo_path = os.path.abspath(repo_path_raw) if repo_path_raw else workspace_path
    focus = str(params.get("focus", "")).strip()

    from backend.tutorializer.generator import TutorialGenerator
    from pathlib import Path

    repo_dir = Path(repo_path)
    if not repo_dir.is_dir():
        raise ValueError(f"Repository path does not exist or is not a directory: {repo_path}")

    store = _get_knowledge_store(server)
    generator = TutorialGenerator(
        repo_path=repo_dir,
        model=_resolve_tutorialize_model(server, workspace_path),
        store=store,
    )

    # Phase 1: repo analysis
    analysis = generator.analyze_repo()
    key_areas = analysis.get("key_areas", [])
    overview = analysis.get("overview", "")
    stack = analysis.get("stack", [])

    # Stamp stack onto each area for later tag generation
    for area in key_areas:
        area["_stack"] = stack

    # Phase 2: focus filtering (only if a focus was given)
    if focus and key_areas:
        filtered = generator.filter_areas_by_focus(key_areas, focus)
        if filtered:
            key_areas = filtered

    # Persist analysis so generateTutorials can use it without re-scanning
    session_id = str(uuid.uuid4())
    if not hasattr(server, "_tutorialize_sessions"):
        server._tutorialize_sessions = {}
    server._tutorialize_sessions[session_id] = {
        "repo_path": repo_path,
        "workspace_path": workspace_path,
        "focus": focus,
        "key_areas": key_areas,
        "overview": overview,
        "stack": stack,
    }

    # Strip internal _stack before sending to frontend
    clean_areas = [
        {k: v for k, v in area.items() if k != "_stack"}
        for area in key_areas
    ]

    return {
        "sessionId": session_id,
        "overview": overview,
        "areas": clean_areas,
    }


def handle_generate_tutorials(server, params: dict) -> dict:
    """
    Phase 3 + 4: generate tutorials for the selected areas and store them.

    Params:
        sessionId  — from analyzeTutorializeRepo
        areas      — list of area names to generate (empty = generate all)
    """
    session_id = str(params.get("sessionId", "")).strip()
    requested_areas = [str(a).strip() for a in params.get("areas", []) if str(a).strip()]

    if not hasattr(server, "_tutorialize_sessions") or session_id not in server._tutorialize_sessions:
        raise ValueError(f"Tutorialize session not found: {session_id!r}")

    session = server._tutorialize_sessions[session_id]
    repo_path = session.get("repo_path") or session.get("workspace_path", ".")
    workspace_path = session["workspace_path"]
    focus = session["focus"]
    key_areas = list(session["key_areas"])

    # Filter to requested areas if specified
    if requested_areas:
        requested_set = {a.lower() for a in requested_areas}
        filtered = [a for a in key_areas if a.get("name", "").lower() in requested_set]
        if filtered:
            key_areas = filtered

    from backend.tutorializer.generator import TutorialGenerator
    from pathlib import Path

    progress_messages: list[str] = []
    store = _get_knowledge_store(server)
    generator = TutorialGenerator(
        repo_path=Path(repo_path),
        model=_resolve_tutorialize_model(server, workspace_path),
        store=store,
        progress_cb=lambda msg: progress_messages.append(msg),
    )

    added = 0
    tutorial_titles: list[str] = []
    for i, area in enumerate(key_areas, 1):
        try:
            entry = generator.generate_tutorial_for_area(area, focus)
            if entry is None:
                continue
            if store.add_entry(entry):
                added += 1
                tutorial_titles.append(entry.title)
        except Exception as exc:
            log.error("Skipping area '%s': %s", area.get("name"), exc)
            progress_messages.append(f"Skipped {area.get('name')}: {exc}")

    # Register repo in knowledge sources
    from backend.tutorializer import scanner
    repo_dir = Path(repo_path)
    store.upsert_repo(
        name=repo_dir.name,
        local_path=repo_path,
        remote_url=scanner.get_git_remote(repo_dir),
    )

    if tutorial_titles:
        summary = (
            f"Generated {added} tutorial(s):\n"
            + "\n".join(f"  • {t}" for t in tutorial_titles)
        )
    else:
        summary = "No new tutorials were added (they may already be in the knowledge base)."

    # Store tutorial titles in session for follow-up Q&A context
    session["tutorial_titles"] = tutorial_titles

    return {
        "added": added,
        "summary": summary,
        "progressMessages": progress_messages,
    }


def handle_tutorialize_chat(server, params: dict) -> dict:
    """
    Follow-up Q&A: search the knowledge base for relevant tutorials and answer
    the user's question using the runtime's answer_question method.
    """
    session_id = str(params.get("sessionId", "")).strip()
    message = str(params.get("message", "")).strip()

    if not message:
        return {"answer": ""}

    workspace_path = "."
    focus = ""
    context_hint = ""

    repo_path = workspace_path
    if hasattr(server, "_tutorialize_sessions") and session_id in server._tutorialize_sessions:
        sess = server._tutorialize_sessions[session_id]
        workspace_path = sess.get("workspace_path", ".")
        repo_path = sess.get("repo_path") or workspace_path
        focus = sess.get("focus", "")
        titles = sess.get("tutorial_titles", [])
        if titles:
            context_hint = "Available tutorials:\n" + "\n".join(f"  - {t}" for t in titles)

    # Search the knowledge base for relevant tutorial entries
    knowledge_context = ""
    store = _get_knowledge_store(server)
    try:
        entries = store.search(message, limit=4)
        if entries:
            snippets = []
            for entry in entries:
                snippets.append(f"### {entry.title}\n{entry.code[:1200]}")
            knowledge_context = "\n\n".join(snippets)
    except Exception as exc:
        log.warning("Knowledge search failed during tutorialize chat: %s", exc)

    context_parts = []
    import os as _os
    repo_name = _os.path.basename(repo_path.rstrip("/\\")) if repo_path else ""
    if repo_name:
        context_parts.append(f"Repository being tutorialized: {repo_name} ({repo_path})")
    if context_hint:
        context_parts.append(context_hint)
    if knowledge_context:
        context_parts.append("Relevant tutorial content:\n\n" + knowledge_context)
    if focus:
        context_parts.append(f"The user's original learning focus: {focus}")

    context = "\n\n".join(context_parts) or "No specific tutorial context available."

    runtime = server._get_runtime(workspace_path)
    result = runtime.answer_question(
        question=message,
        context=context,
        workspace_path=workspace_path,
        persona="tutorializer",
    )

    answer = str(result.get("answer", result.get("text", "I don't have enough context to answer that.")))
    return {"answer": answer}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_tutorialize_model(server, workspace_path: str) -> str:
    """Pick the Ollama model to use for tutorialization.

    Prefers the workspace provider profile's knowledge-stage model when available,
    falls back to llama3.2.
    """
    try:
        profile = server._get_provider_profile(workspace_path)
        catalog = getattr(profile, "catalog", []) or []
        for entry in catalog:
            provider_type = getattr(entry, "type", "") or ""
            if provider_type == "ollama":
                models = getattr(entry, "models", []) or []
                if models:
                    return models[0]
    except Exception:
        pass
    return "llama3.2"
