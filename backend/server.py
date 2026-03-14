"""
WaterFree backend — stdio JSON-RPC server.

Protocol: newline-delimited JSON on stdin/stdout.
  → {"id": "1", "method": "methodName", "params": {...}}
  ← {"id": "1", "result": {...}}
  ← {"id": "1", "error": {"code": -32000, "message": "..."}}

Methods:
  indexWorkspace       {path}                               → IndexSummary
  createSession        {goal, workspacePath}                → PlanDocument
  getSession           {sessionId?, workspacePath?}         → PlanDocument | null
  saveSession          {session}                            → {ok}
  generatePlan         {goal, sessionId?, workspacePath}    → {tasks, questions, sessionId}
  generateAnnotation   {taskId, sessionId}                  → IntentAnnotation
  approveAnnotation    {annotationId, sessionId}            → {ok}
  alterAnnotation      {taskId, annotationId, feedback, sessionId} → IntentAnnotation
  finalizeExecution    {sessionId, taskId, diagnostics[]}   → {ok, status, blockingDiagnostics}
  redirectTask         {taskId, instruction, sessionId}     → Task
  skipTask             {taskId, sessionId}                  → PlanDocument
  queueTodoInstruction {file, line, instruction, sessionId} → {queued, taskId}
  listTasks            {workspacePath, ...filters}          → {tasks, phases, updatedAt, path}
  getTaskBoard         {workspacePath}                      → {tasks, phases, updatedAt, path}
  searchTasks          {workspacePath, query, limit}        → {tasks, count, path}
  addTask              {workspacePath, task}                → {task, path}
  updateTask           {workspacePath, taskId, patch}       → {task, path}
  deleteTask           {workspacePath, taskId}              → {ok, deleted}
  saveTaskBoard        {workspacePath, tasks, phases}       → {tasks, phases, updatedAt, path}
  whatNext             {workspacePath, ownerName?}          → {task|null, path}
  liveDebug            {debugContext, sessionId?, workspacePath} → DebugAnalysis
  updateFile           {path, workspacePath}                → {ok}
  removeFile           {path, workspacePath}                → {ok}

Run as:
  python -m backend.server
"""

from __future__ import annotations
import json
import logging
import os
import sys
from typing import Any, Optional

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _resolve_backend_log_file() -> str:
    explicit_path = os.environ.get("WATERFREE_BACKEND_LOG_FILE")
    if explicit_path:
        return explicit_path
    return os.path.join(os.getcwd(), ".waterfree", "logs", "backend.log")


def _configure_logging() -> tuple[logging.Logger, str]:
    log_file = _resolve_backend_log_file()
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        handlers=handlers,
        force=True,
    )
    logger = logging.getLogger("waterfree.server")
    logger.info("Backend logging initialized: %s", log_file)
    return logger, log_file


log, BACKEND_LOG_FILE = _configure_logging()


def _log_uncaught_exception(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    log.critical("Uncaught backend exception", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = _log_uncaught_exception

from backend.graph.client import GraphClient
from backend.graph.index_state_store import IndexStateStore
from backend.knowledge.store import KnowledgeStore
from backend.llm.context_lifecycle import ContextLifecycleManager
from backend.llm.provider_profiles import (
    ProviderProfileDocument,
    default_provider_profile_document,
    load_provider_profile,
    normalize_provider_profile,
)
from backend.llm.prompt_templates import set_persona_prompt_overrides
from backend.llm.provider_resolver import resolve_provider
from backend.llm.runtime import AgentRuntime
from backend.llm.runtime_registry import (
    create_runtime,
    resolve_runtime_name,
)
from backend.session.models import PlanDocument
from backend.session.session_manager import SessionManager
from backend.todo.store import TaskStore
from backend.wizard import WizardManager

# Handler imports
from backend.handlers.index_handler import (
    handle_index_workspace,
    handle_update_file,
    handle_remove_file,
)
from backend.handlers.session_handler import (
    handle_create_session,
    handle_get_session,
    handle_save_session,
    handle_list_archived_sessions,
    handle_restore_session,
)
from backend.handlers.task_handler import (
    handle_generate_plan,
    handle_generate_annotation,
    handle_approve_annotation,
    handle_alter_annotation,
    handle_execute_task,
    handle_finalize_execution,
    handle_redirect_task,
    handle_skip_task,
    handle_queue_todo_instruction,
)
from backend.handlers.todo_handler import (
    handle_list_tasks,
    handle_get_task_board,
    handle_search_tasks,
    handle_add_task,
    handle_update_task,
    handle_delete_task,
    handle_save_task_board,
    handle_what_next,
)
from backend.handlers.wizard_handler import (
    handle_create_wizard_session,
    handle_get_wizard_session,
    handle_run_wizard_step,
    handle_accept_wizard_chunk,
    handle_accept_wizard_step,
    handle_start_wizard_coding,
    handle_promote_wizard_todos,
    handle_run_wizard_review,
)
from backend.handlers.debug_handler import handle_live_debug
from backend.handlers.runtime_handler import (
    handle_list_runtimes,
    handle_get_active_runtime,
    handle_set_active_runtime,
    handle_sync_provider_profile,
    handle_list_personas,
    handle_save_personas,
    handle_list_skills,
    handle_reload_skills,
    handle_get_skill_detail,
    handle_list_checkpoints,
    handle_resume_checkpoint,
    handle_discard_checkpoint,
    handle_list_subagents,
    handle_delegate_to_subagent,
    handle_get_usage_stats,
    handle_get_provider_capabilities,
)
from backend.handlers.graph_handler import (
    handle_get_adr,
    handle_store_adr,
    handle_update_adr,
    handle_delete_adr,
    handle_list_projects,
    handle_delete_project,
    handle_index_status,
    handle_get_graph_schema,
)
from backend.handlers.memory_handler import (
    handle_get_memory,
    handle_save_memory,
    handle_get_context_lifecycle,
    handle_reset_context_lifecycle,
    handle_build_knowledge,
    handle_add_knowledge_repo,
    handle_search_knowledge,
    handle_list_knowledge_sources,
    handle_browse_knowledge_index,
    handle_extract_procedure,
    handle_remove_knowledge_source,
    handle_add_knowledge_entry,
    handle_delete_knowledge_entry,
)


class Server:
    def __init__(self):
        # GraphClient wraps the internal GraphEngine (no external binary needed).
        self._graph = GraphClient()
        self._sessions: dict[str, PlanDocument] = {}        # sessionId -> PlanDocument
        self._session_managers: dict[str, SessionManager] = {}  # path -> SessionManager
        self._index_state_stores: dict[str, IndexStateStore] = {}  # path -> index state
        self._task_stores: dict[str, TaskStore] = {}  # path -> workspace task store
        self._wizard_managers: dict[str, WizardManager] = {}  # path -> wizard artifact manager
        self._runtime_cache: dict[tuple[str, str, str], AgentRuntime] = {}
        self._knowledge_store: Optional[KnowledgeStore] = None
        self._context_lifecycle = ContextLifecycleManager()
        self._runtime_name = resolve_runtime_name()
        self._provider_profiles: dict[str, ProviderProfileDocument] = {}

    def _get_runtime(
        self,
        workspace_path: str = ".",
        *,
        profile_override: ProviderProfileDocument | None = None,
    ) -> AgentRuntime:
        workspace = os.path.abspath(workspace_path)
        profile = profile_override or self._get_provider_profile(workspace)
        runtime_name = getattr(self, "_runtime_name", "deep_agents")
        cache_key = (workspace, runtime_name, profile.profile_hash)
        if not hasattr(self, "_runtime_cache"):
            self._runtime_cache = {}
        runtime = self._runtime_cache.get(cache_key)
        if runtime is None:
            runtime = create_runtime(
                runtime_name=runtime_name,
                graph=self._graph,
                knowledge_store=self._knowledge_store,
                task_store_factory=self._get_task_store,
                workspace_path=workspace,
                provider_profile_document=profile,
            )
            self._runtime_cache[cache_key] = runtime
        return runtime

    def _get_provider_profile_for_session(self, doc: PlanDocument) -> ProviderProfileDocument:
        profile = self._get_provider_profile(doc.workspace_path)
        selection = getattr(doc, "runtime_selection", None)
        if selection is None:
            return profile

        provider_id = selection.provider_id.strip()
        model = selection.model.strip()
        if not provider_id and not model:
            return profile

        raw = profile.to_dict()
        catalog = raw.get("catalog", [])
        if not isinstance(catalog, list) or not catalog:
            return profile

        available_ids = {
            str(entry.get("id", "")).strip()
            for entry in catalog
            if isinstance(entry, dict)
        }
        if provider_id and provider_id not in available_ids:
            return profile

        target_provider_id = provider_id or str(raw.get("activeProviderId", "")).strip()
        if provider_id:
            raw["activeProviderId"] = provider_id

        if model and target_provider_id:
            for entry in catalog:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("id", "")).strip() != target_provider_id:
                    continue
                models = entry.get("models", {})
                if not isinstance(models, dict):
                    models = {}
                for stage_name in (
                    "default",
                    "planning",
                    "annotation",
                    "execution",
                    "debug",
                    "question_answer",
                    "alter_annotation",
                    "ripple_detection",
                    "knowledge",
                ):
                    models[stage_name] = model
                entry["models"] = models
                break

        return normalize_provider_profile(raw)

    def _get_runtime_for_session(self, doc: PlanDocument) -> AgentRuntime:
        return self._get_runtime(
            doc.workspace_path,
            profile_override=self._get_provider_profile_for_session(doc),
        )

    def _get_runtime_for_stage(self, workspace_path: str, *, stage: str, workload: str = "") -> AgentRuntime:
        workspace = os.path.abspath(workspace_path)
        profile = self._get_provider_profile(workspace)
        resolved = resolve_provider(
            profile,
            stage=stage,
            persona="default",
            preferred_runtime="ollama" if "knowledge" in workload.strip().lower() or stage.strip().lower() in {"snippetize", "knowledge"} else "",
        )
        runtime_name = resolved.runtime_name if resolved is not None else self._runtime_name
        return create_runtime(
            runtime_name=runtime_name,
            graph=self._graph,
            knowledge_store=self._knowledge_store,
            task_store_factory=self._get_task_store,
            workspace_path=workspace,
            provider_profile_document=profile,
        )

    def _get_knowledge_store(self) -> KnowledgeStore:
        if not self._knowledge_store:
            self._knowledge_store = KnowledgeStore()
        return self._knowledge_store

    def _get_session_manager(self, workspace_path: str) -> SessionManager:
        path = os.path.abspath(workspace_path)
        if path not in self._session_managers:
            self._session_managers[path] = SessionManager(path)
        return self._session_managers[path]

    def _get_task_store(self, workspace_path: str) -> TaskStore:
        path = os.path.abspath(workspace_path)
        if path not in self._task_stores:
            self._task_stores[path] = TaskStore(path)
        return self._task_stores[path]

    def _get_index_state_store(self, workspace_path: str) -> IndexStateStore:
        path = os.path.abspath(workspace_path)
        if path not in self._index_state_stores:
            self._index_state_stores[path] = IndexStateStore(path)
        return self._index_state_stores[path]

    def _get_wizard_manager(self, workspace_path: str, public_docs_path: str = "docs") -> WizardManager:
        path = os.path.abspath(workspace_path)
        if path not in self._wizard_managers:
            self._wizard_managers[path] = WizardManager(path, public_docs_path=public_docs_path)
        else:
            self._wizard_managers[path].set_public_docs_path(public_docs_path)
        return self._wizard_managers[path]

    def _get_session(self, session_id: str) -> Optional[PlanDocument]:
        return self._sessions.get(session_id)

    def _get_provider_profile(self, workspace_path: str) -> ProviderProfileDocument:
        path = os.path.abspath(workspace_path)
        if not hasattr(self, "_provider_profiles"):
            self._provider_profiles = {}
        profile = self._provider_profiles.get(path)
        if profile is None:
            profile = load_provider_profile(path)
            if not profile.catalog:
                runtime_name = getattr(self, "_runtime_name", "deep_agents")
                default_type = "openai" if runtime_name == "openai" else "ollama" if runtime_name == "ollama" else "claude"
                profile = default_provider_profile_document(default_type)
            self._provider_profiles[path] = profile
            set_persona_prompt_overrides(profile.policies.persona_prompt_overrides)
        return profile

    def _set_provider_profile(self, workspace_path: str, profile: ProviderProfileDocument) -> str:
        path = os.path.abspath(workspace_path)
        if not hasattr(self, "_provider_profiles"):
            self._provider_profiles = {}
        normalized = normalize_provider_profile(profile.to_dict())
        self._provider_profiles[path] = normalized
        set_persona_prompt_overrides(normalized.policies.persona_prompt_overrides)
        self._clear_runtime_cache(workspace_path=path)
        return normalized.profile_hash

    def _clear_runtime_cache(self, workspace_path: str | None = None) -> None:
        if not hasattr(self, "_runtime_cache"):
            self._runtime_cache = {}
        if workspace_path is None:
            self._runtime_cache.clear()
            return
        abs_path = os.path.abspath(workspace_path)
        self._runtime_cache = {
            key: value for key, value in self._runtime_cache.items()
            if key[0] != abs_path
        }

    def _require_session(self, session_id: str) -> PlanDocument:
        doc = self._sessions.get(session_id)
        if not doc:
            raise ValueError(f"Session not found: {session_id}")
        return doc

    # Kept as a method for backwards compatibility with tests and any callers
    # that invoke it directly on the Server instance.
    def handle_finalize_execution(self, params: dict) -> dict:
        from backend.handlers.task_handler import handle_finalize_execution as _h
        return _h(self, params)

    def close(self) -> None:
        self._graph.close()
        for store in self._task_stores.values():
            store.close()
        if self._knowledge_store:
            self._knowledge_store.close()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    METHODS = {
        "indexWorkspace":       handle_index_workspace,
        "createSession":        handle_create_session,
        "getSession":           handle_get_session,
        "listArchivedSessions": handle_list_archived_sessions,
        "restoreSession":       handle_restore_session,
        "createWizardSession":  handle_create_wizard_session,
        "getWizardSession":     handle_get_wizard_session,
        "runWizardStep":        handle_run_wizard_step,
        "acceptWizardChunk":    handle_accept_wizard_chunk,
        "acceptWizardStep":     handle_accept_wizard_step,
        "promoteWizardTodos":   handle_promote_wizard_todos,
        "startWizardCoding":    handle_start_wizard_coding,
        "runWizardReview":      handle_run_wizard_review,
        "saveSession":          handle_save_session,
        "generatePlan":         handle_generate_plan,
        "generateAnnotation":   handle_generate_annotation,
        "approveAnnotation":    handle_approve_annotation,
        "executeTask":          handle_execute_task,
        "finalizeExecution":    handle_finalize_execution,
        "alterAnnotation":      handle_alter_annotation,
        "redirectTask":         handle_redirect_task,
        "skipTask":             handle_skip_task,
        "queueTodoInstruction": handle_queue_todo_instruction,
        "listTasks":            handle_list_tasks,
        "getTaskBoard":         handle_get_task_board,
        "searchTasks":          handle_search_tasks,
        "addTask":              handle_add_task,
        "updateTask":           handle_update_task,
        "deleteTask":           handle_delete_task,
        "saveTaskBoard":        handle_save_task_board,
        "whatNext":             handle_what_next,
        "liveDebug":            handle_live_debug,
        "updateFile":           handle_update_file,
        "removeFile":           handle_remove_file,
        # Runtime management
        "listRuntimes":         handle_list_runtimes,
        "getActiveRuntime":     handle_get_active_runtime,
        "setActiveRuntime":     handle_set_active_runtime,
        "syncProviderProfile":  handle_sync_provider_profile,
        "listPersonas":         handle_list_personas,
        "savePersonas":         handle_save_personas,
        "listSkills":           handle_list_skills,
        "reloadSkills":         handle_reload_skills,
        "getSkillDetail":       handle_get_skill_detail,
        "listCheckpoints":      handle_list_checkpoints,
        "resumeCheckpoint":     handle_resume_checkpoint,
        "discardCheckpoint":    handle_discard_checkpoint,
        "getUsageStats":           handle_get_usage_stats,
        "getProviderCapabilities": handle_get_provider_capabilities,
        "listSubagents":           handle_list_subagents,
        "delegateToSubagent":      handle_delegate_to_subagent,
        # ADR management
        "getADR":               handle_get_adr,
        "storeADR":             handle_store_adr,
        "updateADR":            handle_update_adr,
        "deleteADR":            handle_delete_adr,
        # Graph/project management
        "listProjects":         handle_list_projects,
        "deleteProject":        handle_delete_project,
        "indexStatus":          handle_index_status,
        "getGraphSchema":       handle_get_graph_schema,
        # Project memory
        "getMemory":            handle_get_memory,
        "saveMemory":           handle_save_memory,
        # Context lifecycle
        "getContextLifecycle":  handle_get_context_lifecycle,
        "resetContextLifecycle": handle_reset_context_lifecycle,
        # Global knowledge base
        "buildKnowledge":           handle_build_knowledge,
        "addKnowledgeRepo":         handle_add_knowledge_repo,
        "extractProcedure":         handle_extract_procedure,
        "searchKnowledge":          handle_search_knowledge,
        "listKnowledgeSources":     handle_list_knowledge_sources,
        "browseKnowledgeIndex":     handle_browse_knowledge_index,
        "removeKnowledgeSource":    handle_remove_knowledge_source,
        "addKnowledgeEntry":        handle_add_knowledge_entry,
        "deleteKnowledgeEntry":     handle_delete_knowledge_entry,
    }

    def dispatch(self, request: dict) -> dict:
        req_id = request.get("id", "unknown")
        method = request.get("method", "")
        params = request.get("params", {})

        handler = self.METHODS.get(method)
        if not handler:
            return _error(req_id, -32601, f"Method not found: {method}")

        try:
            log.info("Handling request id=%s method=%s", req_id, method)
            result = handler(self, params)
            log.info("Completed request id=%s method=%s", req_id, method)
            return {"id": req_id, "result": result}
        except Exception as e:
            log.exception("Error handling %s", method)
            return _error(req_id, -32000, str(e))


# ------------------------------------------------------------------
# stdio loop
# ------------------------------------------------------------------

def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _write_notification(method: str, params: dict) -> None:
    _write({"method": method, "params": params})


def _blocking_diagnostic_summary(diagnostic: dict, total: int) -> str:
    location = _diagnostic_location(diagnostic)
    message = str(diagnostic.get("message", "")).strip()
    if total <= 1:
        return f"{location} — {message}" if location else message
    prefix = f"{location} — " if location else ""
    return f"{prefix}{message} (+{total - 1} more)"


def _blocking_diagnostic_details(diagnostics: list[dict]) -> str:
    parts = []
    for diagnostic in diagnostics[:3]:
        location = _diagnostic_location(diagnostic)
        message = str(diagnostic.get("message", "")).strip()
        source = str(diagnostic.get("source", "")).strip()
        prefix = f"{location}: " if location else ""
        suffix = f" [{source}]" if source else ""
        parts.append(f"{prefix}{message}{suffix}")
    if len(diagnostics) > 3:
        parts.append(f"... plus {len(diagnostics) - 3} more")
    return "; ".join(parts)


def _diagnostic_location(diagnostic: dict) -> str:
    file_path = str(diagnostic.get("file", "")).strip()
    line = diagnostic.get("line")
    if file_path and line:
        return f"{file_path}:{line}"
    if file_path:
        return file_path
    if line:
        return f"line {line}"
    return ""


def workspace_path_from_source(source: str) -> str:
    if os.path.isdir(source):
        return os.path.abspath(source)
    return os.getcwd()


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"id": req_id, "error": {"code": code, "message": message}}


def run() -> None:
    log.info(
        "WaterFree backend starting (stdin/stdout JSON-RPC, logFile=%s)",
        BACKEND_LOG_FILE,
    )
    server = Server()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as e:
                log.error("JSON parse error: %s", e)
                _write(_error("unknown", -32700, f"Parse error: {e}"))
                continue

            response = server.dispatch(request)
            _write(response)
    finally:
        log.info("WaterFree backend shutting down")
        server.close()


if __name__ == "__main__":
    run()
