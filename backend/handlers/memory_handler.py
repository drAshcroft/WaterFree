"""Memory, context lifecycle, and knowledge base handlers."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("waterfree.server")


def _write_notification(method: str, params: dict) -> None:
    from backend.server import _write_notification as _wn
    _wn(method, params)


def handle_get_memory(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    memory_path = os.path.join(workspace_path, ".waterfree", "memory.md")
    try:
        with open(memory_path, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
    return {"content": content, "path": memory_path}


def handle_save_memory(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    content = params.get("content", "")
    pairs_dir = os.path.join(workspace_path, ".waterfree")
    os.makedirs(pairs_dir, exist_ok=True)
    memory_path = os.path.join(pairs_dir, "memory.md")
    with open(memory_path, "w", encoding="utf-8") as f:
        f.write(content)
    log.info("memory.md saved (%d chars)", len(content))
    return {"ok": True, "path": memory_path}


def handle_get_context_lifecycle(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    return server._context_lifecycle.inspect(workspace_path)


def handle_reset_context_lifecycle(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    session_id = params.get("sessionId")
    return server._context_lifecycle.reset(workspace_path, session_id=session_id)


def handle_build_knowledge(server, params: dict) -> dict:
    workspace_path = os.path.abspath(params.get("workspacePath", "."))
    focus = params.get("focus", "").strip()
    store = server._get_knowledge_store()

    from backend.knowledge.git_importer import _extract_symbols
    from backend.knowledge.extractor import KnowledgeExtractor

    repo_name = os.path.basename(workspace_path)
    symbols = _extract_symbols(workspace_path)
    total = len(symbols)

    if not symbols:
        return {"added": 0, "message": "No indexable symbols found in workspace."}

    _write_notification("indexProgress", {"done": 0, "total": total, "phase": "knowledge"})

    def progress(done: int, tot: int) -> None:
        _write_notification("indexProgress", {"done": done, "total": tot, "phase": "knowledge"})

    runtime = server._get_runtime_for_stage(
        workspace_path,
        stage="knowledge",
        workload="knowledge extraction",
    )

    extractor = KnowledgeExtractor(
        store=store,
        runtime=runtime,
        source_repo=repo_name,
        focus=focus,
        workspace_path=workspace_path,
        progress_cb=progress,
    )
    added = extractor.extract_from_symbols(symbols)
    store.upsert_repo(repo_name, workspace_path)

    _write_notification("indexProgress", {"done": total, "total": total, "phase": "knowledge"})
    log.info("buildKnowledge: %s — %d new entries added", repo_name, added)
    return {
        "added": added,
        "symbolsScanned": total,
        "repo": repo_name,
        "message": f"Added {added} new patterns to global knowledge base.",
    }


def handle_add_knowledge_repo(server, params: dict) -> dict:
    source = params.get("source", "").strip()
    if not source:
        raise ValueError("source is required (git URL or local path)")

    focus = params.get("focus", "").strip()
    store = server._get_knowledge_store()

    from backend.knowledge.git_importer import import_repo
    from backend.server import workspace_path_from_source

    def progress(done: int, total: int) -> None:
        _write_notification("indexProgress", {"done": done, "total": total, "phase": "knowledge"})

    runtime = server._get_runtime_for_stage(
        source if os.path.isdir(source) else workspace_path_from_source(source),
        stage="knowledge",
        workload="knowledge extraction",
    )
    result = import_repo(source, store, runtime=runtime, focus=focus, progress_cb=progress)
    log.info("addKnowledgeRepo: %s", result)
    return result


def handle_search_knowledge(server, params: dict) -> dict:
    query = params.get("query", "")
    limit = int(params.get("limit", 10))
    store = server._get_knowledge_store()
    entries = store.search(query, limit=limit)
    return {
        "entries": [e.to_dict() for e in entries],
        "count": len(entries),
        "total": store.total_entries(),
    }


def handle_list_knowledge_sources(server, params: dict) -> dict:
    store = server._get_knowledge_store()
    repos = store.list_repos()
    return {
        "repos": [r.to_dict() for r in repos],
        "totalEntries": store.total_entries(),
    }


def handle_browse_knowledge_index(server, params: dict) -> dict:
    store = server._get_knowledge_store()
    return store.browse_hierarchy(
        path=str(params.get("path", "")),
        depth=int(params.get("depth", 2)),
        include_entries=bool(params.get("includeEntries", False)),
        entry_limit=int(params.get("entryLimit", 10)),
    )


def handle_extract_procedure(server, params: dict) -> dict:
    name = params.get("name", "").strip()
    if not name:
        raise ValueError("name is required (function or method name)")

    workspace_path = params.get("workspacePath", ".")
    focus = params.get("focus", "").strip()
    max_depth = int(params.get("maxDepth", 3))

    try:
        status = server._graph.index_status(repo_path=workspace_path)
        if status.get("status") == "not_indexed":
            server._graph.index(os.path.abspath(workspace_path))
    except Exception as e:
        log.warning("handle_extract_procedure: index check failed: %s", e)

    from backend.knowledge.procedure_extractor import extract_procedure

    source_repo = os.path.basename(os.path.abspath(workspace_path))
    store = server._get_knowledge_store()
    runtime = server._get_runtime_for_stage(
        workspace_path,
        stage="knowledge",
        workload="procedure extraction",
    )

    result = extract_procedure(
        graph=server._graph,
        store=store,
        runtime=runtime,
        name=name,
        source_repo=source_repo,
        focus=focus,
        workspace_path=workspace_path,
        max_depth=max_depth,
    )
    log.info(
        "extractProcedure: '%s' — kept=%s, nodes=%d, skipped=%d, warnings=%d",
        name,
        result.get("kept"),
        result.get("nodesIncluded", 0),
        result.get("nodesSkipped", 0),
        len(result.get("warnings", [])),
    )
    return result


def handle_remove_knowledge_source(server, params: dict) -> dict:
    name = params.get("name", "").strip()
    if not name:
        raise ValueError("name is required")
    store = server._get_knowledge_store()
    deleted = store.delete_repo(name)
    log.info("removeKnowledgeSource: '%s' — %d entries deleted", name, deleted)
    return {"name": name, "deleted": deleted}


def handle_add_knowledge_entry(server, params: dict) -> dict:
    from backend.knowledge.models import KnowledgeEntry
    store = server._get_knowledge_store()
    entry = KnowledgeEntry.create(
        source_repo=params.get("sourceRepo", params.get("source_repo", "")).strip(),
        source_file=params.get("sourceFile", params.get("source_file", "")).strip(),
        snippet_type=params.get("snippetType", params.get("snippet_type", "pattern")).strip(),
        title=params.get("title", "").strip(),
        description=params.get("description", "").strip(),
        code=params.get("code", "").strip(),
        tags=params.get("tags", []),
        source_repo_url=params.get("sourceRepoUrl", params.get("source_repo_url", "")).strip(),
        context=params.get("context", "").strip(),
        hierarchy_path=params.get("hierarchyPath", params.get("hierarchy_path", None)),
    )
    if not entry.title or not entry.code or not entry.source_repo:
        raise ValueError("title, code, and source_repo are required")
    added = store.add_entry(entry)
    if added:
        store.upsert_repo(entry.source_repo, "")
    log.info("addKnowledgeEntry: '%s' added=%s", entry.title, added)
    return {"id": entry.id, "added": added, "message": "Entry added." if added else "Duplicate entry (same code hash)."}


def handle_delete_knowledge_entry(server, params: dict) -> dict:
    entry_id = params.get("id", "").strip()
    if not entry_id:
        raise ValueError("id is required")
    store = server._get_knowledge_store()
    deleted = store.delete_entry(entry_id)
    log.info("deleteKnowledgeEntry: '%s' deleted=%s", entry_id, deleted)
    return {"id": entry_id, "deleted": deleted}
