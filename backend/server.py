"""
PairProtocol backend — stdio JSON-RPC server.

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
  searchTasks          {workspacePath, query, limit}        → {tasks, count, path}
  addTask              {workspacePath, task}                → {task, path}
  updateTask           {workspacePath, taskId, patch}       → {task, path}
  deleteTask           {workspacePath, taskId}              → {ok, deleted}
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
    explicit_path = os.environ.get("PAIRPROTOCOL_BACKEND_LOG_FILE")
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

from backend.debug.live_debug import DebugContext
from backend.graph.client import GraphClient
from backend.indexer.index_state_store import IndexStateStore
from backend.knowledge.store import KnowledgeStore
from backend.llm.context_builder import ContextBuilder
from backend.llm.context_lifecycle import ContextLifecycleManager
from backend.llm.runtime import AgentRuntime
from backend.llm.runtime_registry import (
    create_runtime,
    list_runtime_descriptors,
    resolve_runtime_name,
)
from backend.negotiation.negotiation_controller import NegotiationController
from backend.negotiation.turn_manager import TurnManager, InvalidTransitionError
from backend.session.models import (
    AIState, AnnotationStatus, IntentAnnotation, PlanDocument, SessionNote, TaskStatus,
)
from backend.session.session_manager import SessionManager
from backend.todo.store import TaskStore
from backend.wizard import WizardManager


class Server:
    def __init__(self):
        # GraphClient wraps the internal GraphEngine (no external binary needed).
        self._graph = GraphClient()
        self._sessions: dict[str, PlanDocument] = {}        # sessionId -> PlanDocument
        self._session_managers: dict[str, SessionManager] = {}  # path -> SessionManager
        self._index_state_stores: dict[str, IndexStateStore] = {}  # path -> index state
        self._task_stores: dict[str, TaskStore] = {}  # path -> workspace task store
        self._wizard_managers: dict[str, WizardManager] = {}  # path -> wizard artifact manager
        self._runtime: Optional[AgentRuntime] = None
        self._claude: Optional[AgentRuntime] = None  # backward-compat alias
        self._knowledge_store: Optional[KnowledgeStore] = None
        self._context_lifecycle = ContextLifecycleManager()
        self._runtime_name = resolve_runtime_name()

    def _get_runtime(self, workspace_path: str = ".") -> AgentRuntime:
        runtime = getattr(self, "_runtime", None) or getattr(self, "_claude", None)
        if not runtime:
            runtime = create_runtime(
                runtime_name=self._runtime_name,
                graph=self._graph,
                knowledge_store=self._knowledge_store,
                task_store_factory=self._get_task_store,
                workspace_path=workspace_path,
            )
            self._runtime = runtime
            self._claude = runtime
        return runtime

    def _get_claude(self) -> AgentRuntime:
        # Compatibility shim for existing call sites/tests while runtime naming settles.
        return self._get_runtime()

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

    # ------------------------------------------------------------------
    # Method handlers
    # ------------------------------------------------------------------

    def handle_index_workspace(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("path", "."))
        store = self._get_index_state_store(workspace_path)
        check = store.quick_check()

        needs_index = not check.has_prior_index or check.changed_count > 0
        reason = "first_index" if not check.has_prior_index else "changed_files"

        if not needs_index:
            # Ensure the graph process has at least one indexed project loaded.
            # If not, re-index once to restore runtime state.
            try:
                status = self._graph.index_status(repo_path=workspace_path)
                if status.get("status") == "not_indexed":
                    needs_index = True
                    reason = "graph_project_missing"
            except Exception as exc:
                log.warning("Graph project check failed; forcing index: %s", exc)
                needs_index = True
                reason = "graph_check_failed"

        if not needs_index:
            return {
                "status": "up_to_date",
                "indexed": False,
                "changedCount": 0,
                "changedPaths": [],
                "workspacePath": workspace_path,
                "dbPath": store.db_path,
                "scannedFiles": check.scanned_files,
            }

        log.info(
            "Indexing workspace via graph: %s (reason=%s, changed=%d)",
            workspace_path,
            reason,
            check.changed_count,
        )
        _write_notification("indexProgress", {"done": 0, "total": 1})
        graph_result = self._graph.index(workspace_path)
        _write_notification("indexProgress", {"done": 1, "total": 1})

        store.record_index(check, reason=reason, index_result=graph_result)

        return {
            "status": "indexed",
            "indexed": True,
            "reason": reason,
            "changedCount": check.changed_count,
            "changedPaths": check.changed_paths,
            "workspacePath": workspace_path,
            "dbPath": store.db_path,
            "scannedFiles": check.scanned_files,
            "graph": graph_result,
        }

    def handle_create_session(self, params: dict) -> dict:
        goal = params["goal"]
        workspace_path = params.get("workspacePath", ".")
        persona = params.get("persona", "default")
        sm = self._get_session_manager(workspace_path)
        doc = sm.create_session(goal, persona=persona)
        self._sessions[doc.id] = doc
        return doc.to_dict()

    def handle_get_session(self, params: dict) -> Optional[dict]:
        session_id = params.get("sessionId")
        if session_id:
            doc = self._sessions.get(session_id)
            return doc.to_dict() if doc else None
        # Try loading from disk
        workspace_path = params.get("workspacePath", ".")
        sm = self._get_session_manager(workspace_path)
        doc = sm.load_session()
        if doc:
            self._sessions[doc.id] = doc
            return doc.to_dict()
        return None

    def handle_create_wizard_session(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        goal = str(params.get("goal", "")).strip()
        public_docs_path = str(params.get("publicDocsPath", "docs")).strip() or "docs"
        wizard_id = str(params.get("wizardId", "bring_idea_to_life")).strip() or "bring_idea_to_life"
        persona = str(params.get("persona", "architect")).strip() or "architect"
        manager = self._get_wizard_manager(workspace_path, public_docs_path=public_docs_path)
        run = manager.create_or_resume_run(goal=goal, wizard_id=wizard_id, persona=persona)
        return {
            "wizard": run.to_dict(),
            "openDocPath": manager.active_doc_path(run),
        }

    def handle_get_wizard_session(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        run_id = str(params.get("runId", "")).strip() or None
        public_docs_path = str(params.get("publicDocsPath", "docs")).strip() or "docs"
        manager = self._get_wizard_manager(workspace_path, public_docs_path=public_docs_path)
        run = manager.get_run(run_id)
        if not run:
            return {"wizard": None, "openDocPath": ""}
        return {
            "wizard": run.to_dict(),
            "openDocPath": manager.active_doc_path(run),
        }

    def handle_run_wizard_step(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        run_id = str(params.get("runId", "")).strip()
        stage_id = str(params.get("stageId", "")).strip()
        if not run_id or not stage_id:
            raise ValueError("runId and stageId are required")
        manager = self._get_wizard_manager(workspace_path)
        runtime = self._get_runtime(workspace_path)
        return manager.run_stage(
            run_id=run_id,
            stage_id=stage_id,
            runtime=runtime,
            revision_note=str(params.get("revisionNote", "")).strip(),
            chunk_id=str(params.get("chunkId", "")).strip(),
            extra_context=str(params.get("extraContext", "")).strip(),
        )

    def handle_accept_wizard_chunk(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        run_id = str(params.get("runId", "")).strip()
        stage_id = str(params.get("stageId", "")).strip()
        chunk_id = str(params.get("chunkId", "")).strip()
        if not run_id or not stage_id or not chunk_id:
            raise ValueError("runId, stageId, and chunkId are required")
        manager = self._get_wizard_manager(workspace_path)
        return manager.accept_chunk(run_id=run_id, stage_id=stage_id, chunk_id=chunk_id)

    def handle_accept_wizard_step(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        run_id = str(params.get("runId", "")).strip()
        stage_id = str(params.get("stageId", "")).strip()
        if not run_id or not stage_id:
            raise ValueError("runId and stageId are required")
        manager = self._get_wizard_manager(workspace_path)
        return manager.accept_stage(run_id=run_id, stage_id=stage_id)

    def handle_start_wizard_coding(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        run_id = str(params.get("runId", "")).strip()
        if not run_id:
            raise ValueError("runId is required")
        manager = self._get_wizard_manager(workspace_path)
        session_manager = self._get_session_manager(workspace_path)
        task_store = self._get_task_store(workspace_path)
        return manager.start_coding(
            run_id=run_id,
            session_manager=session_manager,
            sessions=self._sessions,
            task_store=task_store,
        )

    def handle_promote_wizard_todos(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        run_id = str(params.get("runId", "")).strip()
        if not run_id:
            raise ValueError("runId is required")
        manager = self._get_wizard_manager(workspace_path)
        task_store = self._get_task_store(workspace_path)
        return manager.promote_todos(run_id=run_id, task_store=task_store)

    def handle_run_wizard_review(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        run_id = str(params.get("runId", "")).strip()
        if not run_id:
            raise ValueError("runId is required")
        manager = self._get_wizard_manager(workspace_path)
        run = manager.load_run(run_id)
        review_stage = manager.ensure_review_stage(run)

        extra_context = ""
        if run.linked_session_id and run.linked_session_id in self._sessions:
            session = self._sessions[run.linked_session_id]
            pending = [task.title for task in session.tasks if task.status != TaskStatus.COMPLETE]
            extra_context = "Linked session pending tasks:\n" + "\n".join(f"- {title}" for title in pending)

        runtime = self._get_runtime(workspace_path)
        return manager.run_stage(
            run_id=run_id,
            stage_id=review_stage.id,
            runtime=runtime,
            extra_context=extra_context,
        )

    def handle_generate_plan(self, params: dict) -> dict:
        goal = params["goal"]
        session_id = params.get("sessionId")
        workspace_path = params.get("workspacePath", ".")

        # Get or create session
        doc = self._sessions.get(session_id) if session_id else None
        if not doc:
            sm = self._get_session_manager(workspace_path)
            doc = sm.create_session(goal)
            self._sessions[doc.id] = doc

        # Ensure the graph is indexed (index_repository is idempotent / incremental).
        try:
            status = self._graph.index_status(repo_path=workspace_path)
            if status.get("status") == "not_indexed":
                log.info("No graph index found, indexing now...")
                self._graph.index(os.path.abspath(workspace_path))
        except Exception as e:
            log.warning("Graph index check failed: %s — attempting index", e)
            try:
                self._graph.index(os.path.abspath(workspace_path))
            except Exception as e2:
                log.error("Graph indexing failed: %s", e2)

        ctx = ContextBuilder(self._graph)
        context_str = ctx.build_planning_context(goal, doc)

        runtime = self._get_runtime(workspace_path)
        tasks, questions = runtime.generate_plan(
            goal,
            context_str,
            workspace_path=workspace_path,
            persona=doc.persona,
        )

        doc.tasks = tasks
        for t in tasks:
            # Associate tasks with workspace
            if not t.target_file.startswith("/"):
                t.target_file = os.path.join(workspace_path, t.target_file)

        sm = self._get_session_manager(workspace_path)
        sm.save_session(doc)

        return {
            "sessionId": doc.id,
            "tasks": [t.to_dict() for t in tasks],
            "questions": questions,
        }

    def handle_generate_annotation(self, params: dict) -> dict:
        session_id = params["sessionId"]
        task_id = params["taskId"]

        doc = self._get_session(session_id)
        if not doc:
            raise ValueError(f"Session not found: {session_id}")

        task = next((t for t in doc.tasks if t.id == task_id), None)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        ctx = ContextBuilder(self._graph)
        context_str = ctx.build_annotation_context(task, doc)

        runtime = self._get_runtime(doc.workspace_path)
        annotation = runtime.generate_annotation(
            task,
            context_str,
            workspace_path=doc.workspace_path,
            persona=doc.persona,
        )
        task.annotations.append(annotation)
        task.status = TaskStatus.NEGOTIATING

        sm = self._get_session_manager(doc.workspace_path)
        sm.save_session(doc)

        return annotation.to_dict()

    def handle_approve_annotation(self, params: dict) -> dict:
        session_id = params["sessionId"]
        annotation_id = params["annotationId"]

        doc = self._get_session(session_id)
        if not doc:
            raise ValueError(f"Session not found: {session_id}")

        for task in doc.tasks:
            for ann in task.annotations:
                if ann.id == annotation_id:
                    ann.status = AnnotationStatus.APPROVED
                    sm = self._get_session_manager(doc.workspace_path)
                    sm.save_session(doc)
                    return {"ok": True, "annotationId": annotation_id}

        raise ValueError(f"Annotation not found: {annotation_id}")

    def handle_execute_task(self, params: dict) -> dict:
        """
        Execute an approved annotation — call Claude to produce code edits,
        then return the edit list to the TypeScript side for application via
        vscode.workspace.applyEdit (no files are written by the backend).

        Expects the annotation to already be in APPROVED status (set by
        handle_approve_annotation). The task is only marked COMPLETE after
        the editor applies the edits and finalizeExecution reports clean
        diagnostics.
        """
        session_id = params["sessionId"]
        task_id = params["taskId"]

        doc = self._require_session(session_id)
        sm = self._get_session_manager(doc.workspace_path)

        task = next((t for t in doc.tasks if t.id == task_id), None)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        approved = [a for a in task.annotations if a.status == AnnotationStatus.APPROVED]
        if not approved:
            raise ValueError(
                f"Task '{task.title}' has no approved annotation. "
                "Approve an annotation before calling executeTask."
            )

        tm = TurnManager(doc, sm)

        # Transition to EXECUTING — handle whatever current state we're in
        try:
            tm.begin_execution()          # AWAITING_REVIEW → EXECUTING
        except InvalidTransitionError:
            tm.force(AIState.EXECUTING)   # Recovery: e.g. state was already IDLE

        try:
            ctx = ContextBuilder(self._graph)
            context_str = ctx.build_execution_context(task, doc)
            from datetime import datetime, timezone

            task.status = TaskStatus.EXECUTING
            task.blocked_reason = None
            if not task.started_at:
                task.started_at = datetime.now(timezone.utc).isoformat()

            runtime = self._get_runtime(doc.workspace_path)
            edits = runtime.execute_task(
                task,
                context_str,
                workspace_path=doc.workspace_path,
                persona=doc.persona,
            )

            doc.notes.append(SessionNote(
                timestamp=datetime.now(timezone.utc).isoformat(),
                author="ai",
                text=(
                    f"Task executed: '{task.title}' — "
                    f"{len(edits)} file edit(s) returned to editor for apply/verification."
                ),
            ))

            # Run ripple scan via detect_changes on the working tree.
            # The edits haven't been applied by the editor yet, but this gives a
            # preview of blast radius for any already-uncommitted changes.
            tm.begin_scan()
            scan_context = ctx.build_scan_context(task)
            scan_analysis = runtime.detect_ripple(
                task,
                scan_context,
                workspace_path=doc.workspace_path,
            )
            if scan_analysis:
                doc.notes.append(SessionNote(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    author="ai",
                    text=f"Ripple scan: {scan_analysis}",
                ))

            sm.save_session(doc)

            log.info("executeTask: task '%s' generated %d edit(s)", task.title, len(edits))
            return {
                "edits": edits,
                "taskId": task_id,
                "sessionId": session_id,
                "rippleScan": scan_analysis or "",
            }

        except Exception:
            task.status = TaskStatus.NEGOTIATING
            tm.force(AIState.IDLE)
            raise

    def handle_finalize_execution(self, params: dict) -> dict:
        session_id = params["sessionId"]
        task_id = params["taskId"]
        diagnostics = list(params.get("diagnostics", []))

        doc = self._require_session(session_id)
        task = next((t for t in doc.tasks if t.id == task_id), None)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        sm = self._get_session_manager(doc.workspace_path)
        tm = TurnManager(doc, sm)
        from datetime import datetime, timezone

        blocking = [
            diag for diag in diagnostics
            if str(diag.get("severity", "")).lower() == "error"
        ]

        if blocking:
            task.status = TaskStatus.NEGOTIATING
            task.blocked_reason = _blocking_diagnostic_summary(blocking[0], len(blocking))
            doc.notes.append(SessionNote(
                timestamp=datetime.now(timezone.utc).isoformat(),
                author="system",
                text=(
                    f"Execution blocked for '{task.title}': "
                    f"{_blocking_diagnostic_details(blocking)}"
                ),
            ))
            tm.force(AIState.AWAITING_REVIEW)
            sm.save_session(doc)
            return {
                "ok": False,
                "status": task.status.value,
                "blockingDiagnostics": blocking,
            }

        task.status = TaskStatus.COMPLETE
        task.blocked_reason = None
        task.completed_at = datetime.now(timezone.utc).isoformat()
        doc.notes.append(SessionNote(
            timestamp=task.completed_at,
            author="system",
            text=f"Execution finalized cleanly for '{task.title}'.",
        ))
        try:
            tm.finish()
        except InvalidTransitionError:
            tm.force(AIState.IDLE)
        sm.save_session(doc)
        return {
            "ok": True,
            "status": task.status.value,
            "blockingDiagnostics": [],
        }

    def handle_save_session(self, params: dict) -> dict:
        session_data = params.get("session", {})
        doc = PlanDocument.from_dict(session_data)
        self._sessions[doc.id] = doc
        sm = self._get_session_manager(doc.workspace_path)
        sm.save_session(doc)
        return {"ok": True}

    def handle_alter_annotation(self, params: dict) -> dict:
        session_id = params["sessionId"]
        task_id = params["taskId"]
        annotation_id = params["annotationId"]
        feedback = params["feedback"]

        doc = self._require_session(session_id)
        sm = self._get_session_manager(doc.workspace_path)
        ctrl = NegotiationController(
            doc,
            TurnManager(doc, sm),
            sm,
            self._get_runtime(doc.workspace_path),
            ContextBuilder(self._graph),
        )
        return ctrl.alter_annotation(task_id, annotation_id, feedback)

    def handle_redirect_task(self, params: dict) -> dict:
        session_id = params["sessionId"]
        task_id = params["taskId"]
        instruction = params["instruction"]

        doc = self._require_session(session_id)
        sm = self._get_session_manager(doc.workspace_path)
        ctrl = NegotiationController(
            doc,
            TurnManager(doc, sm),
            sm,
            self._get_runtime(doc.workspace_path),
            ContextBuilder(self._graph),
        )
        return ctrl.redirect_task(task_id, instruction)

    def handle_skip_task(self, params: dict) -> dict:
        session_id = params["sessionId"]
        task_id = params["taskId"]

        doc = self._require_session(session_id)
        sm = self._get_session_manager(doc.workspace_path)
        ctrl = NegotiationController(
            doc,
            TurnManager(doc, sm),
            sm,
            self._get_runtime(doc.workspace_path),
            ContextBuilder(self._graph),
        )
        return ctrl.skip_task(task_id)

    def handle_queue_todo_instruction(self, params: dict) -> dict:
        file_ = params["file"]
        line = int(params.get("line", 0))
        instruction = params["instruction"]
        session_id = params.get("sessionId")
        workspace_path = os.path.abspath(params.get("workspacePath", "."))

        doc: Optional[PlanDocument] = None
        session_result = {"queued": True, "taskId": None}
        if session_id:
            doc = self._require_session(session_id)
            workspace_path = doc.workspace_path
            sm = self._get_session_manager(doc.workspace_path)
            ctrl = NegotiationController(
                doc,
                TurnManager(doc, sm),
                sm,
                self._get_runtime(doc.workspace_path),
                ContextBuilder(self._graph),
            )
            session_result = ctrl.queue_todo_instruction(file_, line, instruction)

        store = self._get_task_store(workspace_path)
        task = store.queue_todo(file_path=file_, line=line, instruction=instruction)
        return {
            **session_result,
            "backlogTaskId": task.id,
            "task": task.to_dict(),
            "path": store.path,
        }

    def handle_list_tasks(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        store = self._get_task_store(workspace_path)
        data = store.list_tasks(
            status=str(params.get("status", "")),
            owner_name=str(params.get("ownerName", "")),
            owner_type=str(params.get("ownerType", "")),
            priority=str(params.get("priority", "")),
            phase=str(params.get("phase", "")),
            ready_only=bool(params.get("readyOnly", False)),
            limit=int(params.get("limit", 100)),
        )
        payload = data.to_dict()
        payload["path"] = store.path
        return payload

    def handle_search_tasks(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        store = self._get_task_store(workspace_path)
        tasks = store.search_tasks(
            query=str(params.get("query", "")),
            limit=int(params.get("limit", 20)),
        )
        return {
            "tasks": [task.to_dict() for task in tasks],
            "count": len(tasks),
            "path": store.path,
        }

    def handle_add_task(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        store = self._get_task_store(workspace_path)
        task_input = params.get("task", params)
        task = store.add_task(task_input)
        return {"task": task.to_dict(), "path": store.path}

    def handle_update_task(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        store = self._get_task_store(workspace_path)
        task_id = str(params.get("taskId", ""))
        if not task_id:
            raise ValueError("taskId is required")
        task = store.update_task(task_id, params.get("patch", {}))
        return {"task": task.to_dict(), "path": store.path}

    def handle_delete_task(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        store = self._get_task_store(workspace_path)
        task_id = str(params.get("taskId", ""))
        if not task_id:
            raise ValueError("taskId is required")
        deleted = store.delete_task(task_id)
        return {"ok": True, "deleted": deleted, "taskId": task_id, "path": store.path}

    def handle_what_next(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        store = self._get_task_store(workspace_path)
        task = store.get_next_task(
            owner_name=str(params.get("ownerName", "")),
            include_unassigned=bool(params.get("includeUnassigned", True)),
        )
        return {"task": task.to_dict() if task else None, "path": store.path}

    def handle_live_debug(self, params: dict) -> dict:
        debug_ctx_dict = params["debugContext"]
        workspace_path = params.get("workspacePath", ".")
        session_id = params.get("sessionId")

        debug_ctx = DebugContext.from_dict({
            **debug_ctx_dict,
            "workspacePath": workspace_path,
        })
        context_str = debug_ctx.format_for_llm()

        doc = self._sessions.get(session_id) if session_id else None
        persona = doc.persona if doc else "default"

        runtime = self._get_runtime(workspace_path)
        analysis = runtime.analyze_debug_context(
            context_str,
            workspace_path=workspace_path,
            persona=persona,
        )

        # If there's an active session, attach a note
        if session_id:
            doc = self._sessions.get(session_id)
            if doc:
                from datetime import datetime, timezone
                doc.notes.append(SessionNote(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    author="ai",
                    text=f"Live debug analysis: {analysis.get('diagnosis', '')}",
                ))
                sm = self._get_session_manager(doc.workspace_path)
                sm.save_session(doc)

        return analysis

    def handle_update_file(self, params: dict) -> dict:
        # codebase-memory-mcp auto-syncs on file changes via its background watcher.
        # This endpoint is kept for protocol compatibility but is now a no-op.
        return {"ok": True}

    def handle_remove_file(self, params: dict) -> dict:
        return {"ok": True}

    def handle_list_runtimes(self, params: dict) -> dict:
        _ = params
        return {"runtimes": [descriptor.to_dict() for descriptor in list_runtime_descriptors()]}

    def handle_get_active_runtime(self, params: dict) -> dict:
        _ = params
        return {"runtimeId": self._runtime_name}

    def handle_set_active_runtime(self, params: dict) -> dict:
        runtime_id = resolve_runtime_name(str(params.get("runtimeId", "")))
        if runtime_id == self._runtime_name:
            return {"ok": True, "runtimeId": self._runtime_name}
        self._runtime_name = runtime_id
        self._runtime = None
        self._claude = None
        return {"ok": True, "runtimeId": self._runtime_name}

    def handle_list_skills(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        persona = str(params.get("persona", ""))
        stage = str(params.get("stage", ""))
        runtime = self._get_runtime(workspace_path)
        list_skills = getattr(runtime, "list_skills", None)
        if callable(list_skills):
            return {"skills": list_skills(persona=persona, stage=stage)}
        from backend.llm.skills.registry import SkillRegistry
        registry = SkillRegistry(workspace_path)
        return {"skills": registry.to_dicts(persona=persona, stage=stage)}

    def handle_reload_skills(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        runtime = self._get_runtime(workspace_path)
        refresh = getattr(runtime, "refresh_skills", None)
        if callable(refresh):
            count = int(refresh())
            return {"ok": True, "count": count}
        from backend.llm.skills.registry import SkillRegistry
        count = len(SkillRegistry(workspace_path).reload())
        return {"ok": True, "count": count}

    def handle_get_skill_detail(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        skill_id = str(params.get("skillId", "")).strip()
        if not skill_id:
            raise ValueError("skillId is required")
        runtime = self._get_runtime(workspace_path)
        get_detail = getattr(runtime, "get_skill_detail", None)
        if callable(get_detail):
            return get_detail(skill_id)
        from backend.llm.skills.registry import SkillRegistry
        return SkillRegistry(workspace_path).get_skill_detail(skill_id)

    def handle_list_checkpoints(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        session_id = str(params.get("sessionId", ""))
        runtime = self._get_runtime(workspace_path)
        list_checkpoints = getattr(runtime, "list_checkpoints", None)
        if not callable(list_checkpoints):
            return {"checkpoints": []}
        checkpoints = list_checkpoints(workspace_path, session_id=session_id)
        return {"checkpoints": checkpoints}

    def handle_resume_checkpoint(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        checkpoint_id = str(params.get("checkpointId", "")).strip()
        if not checkpoint_id:
            raise ValueError("checkpointId is required")
        decision = dict(params.get("decision", {}))
        decision.setdefault("workspacePath", workspace_path)
        runtime = self._get_runtime(workspace_path)
        return {"ok": True, "checkpoint": runtime.resume(checkpoint_id, decision)}

    def handle_discard_checkpoint(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        checkpoint_id = str(params.get("checkpointId", "")).strip()
        if not checkpoint_id:
            raise ValueError("checkpointId is required")
        runtime = self._get_runtime(workspace_path)
        discard = getattr(runtime, "discard", None)
        if not callable(discard):
            raise ValueError("Runtime does not support checkpoint discard.")
        result = discard(checkpoint_id, {"workspacePath": workspace_path})
        return {"ok": bool(result.get("ok", False)), "checkpointId": checkpoint_id}

    def handle_list_subagents(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        runtime = self._get_runtime(workspace_path)
        list_subagents = getattr(runtime, "list_subagents", None)
        if not callable(list_subagents):
            return {"subagents": []}
        return {"subagents": list_subagents()}

    def handle_delegate_to_subagent(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        session_id = str(params.get("sessionId", "")).strip()
        subagent_id = str(params.get("subagentId", "")).strip()
        task_id = str(params.get("taskId", "")).strip()
        prompt = str(params.get("prompt", "")).strip()
        if not session_id or not subagent_id or not task_id:
            raise ValueError("sessionId, subagentId, and taskId are required")
        runtime = self._get_runtime(workspace_path)
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

    # ------------------------------------------------------------------
    # ADR management — persistent architectural memory via the graph
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # .waterfree/memory.md — persistent project notes for every planning prompt
    # ------------------------------------------------------------------

    def handle_get_memory(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        memory_path = os.path.join(workspace_path, ".waterfree", "memory.md")
        try:
            with open(memory_path, encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            content = ""
        return {"content": content, "path": memory_path}

    def handle_save_memory(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        content = params.get("content", "")
        pairs_dir = os.path.join(workspace_path, ".waterfree")
        os.makedirs(pairs_dir, exist_ok=True)
        memory_path = os.path.join(pairs_dir, "memory.md")
        with open(memory_path, "w", encoding="utf-8") as f:
            f.write(content)
        log.info("memory.md saved (%d chars)", len(content))
        return {"ok": True, "path": memory_path}

    def handle_get_context_lifecycle(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        return self._context_lifecycle.inspect(workspace_path)

    def handle_reset_context_lifecycle(self, params: dict) -> dict:
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        session_id = params.get("sessionId")
        return self._context_lifecycle.reset(workspace_path, session_id=session_id)

    def handle_get_adr(self, params: dict) -> dict:
        sections = params.get("sections")
        kwargs = {}
        if sections:
            kwargs["include"] = sections
        return self._graph.manage_adr("get", **kwargs)

    def handle_store_adr(self, params: dict) -> dict:
        content = params.get("content", "")
        if not content:
            raise ValueError("content is required")
        return self._graph.manage_adr("store", content=content)

    def handle_update_adr(self, params: dict) -> dict:
        sections = params.get("sections", {})
        if not sections:
            raise ValueError("sections is required")
        return self._graph.manage_adr("update", sections=sections)

    def handle_delete_adr(self, params: dict) -> dict:
        return self._graph.manage_adr("delete")

    def handle_list_projects(self, params: dict) -> dict:
        return self._graph.list_projects()

    def handle_delete_project(self, params: dict) -> dict:
        return self._graph.delete_project(
            project=params.get("project", ""),
            repo_path=params.get("repoPath", ""),
        )

    def handle_index_status(self, params: dict) -> dict:
        return self._graph.index_status(
            project=params.get("project", ""),
            repo_path=params.get("repoPath", ""),
        )

    def handle_get_graph_schema(self, params: dict) -> dict:
        return self._graph.get_graph_schema(project=params.get("project", ""))

    # ------------------------------------------------------------------
    # Knowledge base — global cross-project snippet store
    # ------------------------------------------------------------------

    def handle_build_knowledge(self, params: dict) -> dict:
        """
        Extract knowledge from the current workspace using LLM classification.
        Runs in the calling thread (already background on the TS side).
        Sends indexProgress notifications as batches complete.
        """
        workspace_path = os.path.abspath(params.get("workspacePath", "."))
        focus = params.get("focus", "").strip()
        store = self._get_knowledge_store()

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

        extractor = KnowledgeExtractor(
            store=store,
            source_repo=repo_name,
            focus=focus,
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

    def handle_add_knowledge_repo(self, params: dict) -> dict:
        """Import a git repo (URL or local path) into the global knowledge base."""
        source = params.get("source", "").strip()
        if not source:
            raise ValueError("source is required (git URL or local path)")

        focus = params.get("focus", "").strip()
        store = self._get_knowledge_store()

        from backend.knowledge.git_importer import import_repo

        def progress(done: int, total: int) -> None:
            _write_notification("indexProgress", {"done": done, "total": total, "phase": "knowledge"})

        result = import_repo(source, store, focus=focus, progress_cb=progress)
        log.info("addKnowledgeRepo: %s", result)
        return result

    def handle_search_knowledge(self, params: dict) -> dict:
        """Search the global knowledge base. Returns matching entries."""
        query = params.get("query", "")
        limit = int(params.get("limit", 10))
        store = self._get_knowledge_store()
        entries = store.search(query, limit=limit)
        return {
            "entries": [e.to_dict() for e in entries],
            "count": len(entries),
            "total": store.total_entries(),
        }

    def handle_list_knowledge_sources(self, params: dict) -> dict:
        """List all indexed knowledge sources."""
        store = self._get_knowledge_store()
        repos = store.list_repos()
        return {
            "repos": [r.to_dict() for r in repos],
            "totalEntries": store.total_entries(),
        }

    def handle_extract_procedure(self, params: dict) -> dict:
        """
        Deep-extract a single named procedure using call chain + data structure assembly.
        The graph must already be indexed for this workspace.
        """
        name = params.get("name", "").strip()
        if not name:
            raise ValueError("name is required (function or method name)")

        workspace_path = params.get("workspacePath", ".")
        focus = params.get("focus", "").strip()
        max_depth = int(params.get("maxDepth", 3))

        # Ensure the graph is indexed for this workspace so find_qualified_name works
        try:
            status = self._graph.index_status(repo_path=workspace_path)
            if status.get("status") == "not_indexed":
                self._graph.index(os.path.abspath(workspace_path))
        except Exception as e:
            log.warning("handle_extract_procedure: index check failed: %s", e)

        from backend.knowledge.procedure_extractor import extract_procedure

        source_repo = os.path.basename(os.path.abspath(workspace_path))
        store = self._get_knowledge_store()

        result = extract_procedure(
            graph=self._graph,
            store=store,
            name=name,
            source_repo=source_repo,
            focus=focus,
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

    def handle_remove_knowledge_source(self, params: dict) -> dict:
        """Delete all knowledge entries for a named source."""
        name = params.get("name", "").strip()
        if not name:
            raise ValueError("name is required")
        store = self._get_knowledge_store()
        deleted = store.delete_repo(name)
        log.info("removeKnowledgeSource: '%s' — %d entries deleted", name, deleted)
        return {"name": name, "deleted": deleted}

    def _require_session(self, session_id: str) -> PlanDocument:
        doc = self._sessions.get(session_id)
        if not doc:
            raise ValueError(f"Session not found: {session_id}")
        return doc

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
        "searchTasks":          handle_search_tasks,
        "addTask":              handle_add_task,
        "updateTask":           handle_update_task,
        "deleteTask":           handle_delete_task,
        "whatNext":             handle_what_next,
        "liveDebug":            handle_live_debug,
        "updateFile":           handle_update_file,
        "removeFile":           handle_remove_file,
        # Runtime management
        "listRuntimes":         handle_list_runtimes,
        "getActiveRuntime":     handle_get_active_runtime,
        "setActiveRuntime":     handle_set_active_runtime,
        "listSkills":           handle_list_skills,
        "reloadSkills":         handle_reload_skills,
        "getSkillDetail":       handle_get_skill_detail,
        "listCheckpoints":      handle_list_checkpoints,
        "resumeCheckpoint":     handle_resume_checkpoint,
        "discardCheckpoint":    handle_discard_checkpoint,
        "listSubagents":        handle_list_subagents,
        "delegateToSubagent":   handle_delegate_to_subagent,
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
        "removeKnowledgeSource":    handle_remove_knowledge_source,
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


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"id": req_id, "error": {"code": code, "message": message}}


def run() -> None:
    log.info(
        "PairProtocol backend starting (stdin/stdout JSON-RPC, logFile=%s)",
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
        log.info("PairProtocol backend shutting down")
        server.close()


if __name__ == "__main__":
    run()
