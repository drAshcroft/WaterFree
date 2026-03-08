from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import json
import uuid

from backend.session.models import PlanDocument
from backend.session.session_manager import SessionManager
from backend.todo.store import TaskStore
from backend.wizard.definitions import (
    ARCHITECT_TEMPLATE,
    CODING_TEMPLATE,
    MARKET_RESEARCH_TEMPLATE,
    REVIEW_TEMPLATE,
    StageTemplate,
    wizard_root,
)
from backend.wizard.document_renderer import DocumentRenderer
from backend.wizard.models import (
    WizardChunkState,
    WizardChunkStatus,
    WizardRun,
    WizardRunStatus,
    WizardStageState,
    WizardStageStatus,
    WizardTodoExport,
)
from backend.wizard.stage_executor import StageExecutor, _normalize_goal_text
from backend.wizard.stage_lifecycle import (
    all_accepted,
    ensure_static_stage,
    is_stage_unlocked,
    on_stage_accepted,
    recompute_current_stage,
)
from backend.wizard.todo_exporter import TodoExporter

_WIZARD_FILENAME = "wizard.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WizardManager:
    def __init__(self, workspace_path: str, public_docs_path: str = "docs"):
        self._workspace = Path(workspace_path).resolve()
        self._wizard_root = self._workspace / ".waterfree" / "wizards"
        self._public_docs_path = public_docs_path.strip() or "docs"
        self._renderer = DocumentRenderer()
        self._executor = StageExecutor()
        self._exporter = TodoExporter()

    @property
    def root_path(self) -> Path:
        return self._wizard_root

    def set_public_docs_path(self, public_docs_path: str) -> None:
        self._public_docs_path = public_docs_path.strip() or "docs"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_or_resume_run(self, *, goal: str, wizard_id: str, persona: str) -> WizardRun:
        if wizard_id != "bring_idea_to_life":
            raise ValueError(f"Wizard '{wizard_id}' is not implemented yet.")

        existing = self._find_latest_active_run()
        cleaned_goal = goal.strip()
        if existing and (not cleaned_goal or existing.goal == cleaned_goal):
            self._migrate_market_research_doc(existing)
            current = existing.get_stage(existing.current_stage_id) or existing.stages[0]
            self._ensure_stage_doc(existing, current)
            market_stage = existing.get_stage(MARKET_RESEARCH_TEMPLATE.id)
            if market_stage and market_stage is not current:
                self._ensure_stage_doc(existing, market_stage)
            return existing

        run_id = str(uuid.uuid4())
        now = _now()
        run_dir = wizard_root(str(self._workspace), run_id)
        market = self._stage_from_template(run_id, MARKET_RESEARCH_TEMPLATE)
        if cleaned_goal:
            idea_chunk = market.get_chunk("initial_goal")
            if idea_chunk:
                idea_chunk.notes_snapshot = cleaned_goal
        architect = self._stage_from_template(run_id, ARCHITECT_TEMPLATE)
        run = WizardRun(
            id=run_id,
            wizard_id=wizard_id,
            goal=cleaned_goal,
            persona=persona or "architect",
            workspace_path=str(self._workspace),
            status=WizardRunStatus.ACTIVE,
            current_stage_id=market.id,
            stages=[market, architect],
            created_at=now,
            updated_at=now,
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_stage_doc(run, market)
        self.save_run(run)
        return run

    def load_run(self, run_id: str) -> WizardRun:
        path = wizard_root(str(self._workspace), run_id) / _WIZARD_FILENAME
        if not path.exists():
            raise ValueError(f"Wizard run not found: {run_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return WizardRun.from_dict(payload)

    def get_run(self, run_id: Optional[str] = None) -> Optional[WizardRun]:
        if run_id:
            return self.load_run(run_id)
        return self._find_latest_active_run()

    def save_run(self, run: WizardRun) -> None:
        run.updated_at = _now()
        run_dir = wizard_root(str(self._workspace), run.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / _WIZARD_FILENAME).write_text(
            json.dumps(run.to_dict(), indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def run_stage(
        self,
        *,
        run_id: str,
        stage_id: str,
        runtime,
        revision_note: str = "",
        chunk_id: str = "",
        extra_context: str = "",
    ) -> dict:
        run = self.load_run(run_id)
        stage = self._require_stage(run, stage_id)
        if not is_stage_unlocked(run, stage):
            raise ValueError(f"Stage '{stage.title}' is locked until earlier stages are accepted.")

        notes_map = self._renderer.load_notes_map(stage)
        self._apply_notes_map(stage, notes_map)
        if stage.id == MARKET_RESEARCH_TEMPLATE.id and extra_context.strip():
            idea_chunk = stage.get_chunk("initial_goal")
            normalized = _normalize_goal_text(extra_context)
            if idea_chunk and normalized:
                idea_chunk.notes_snapshot = normalized
            if normalized:
                run.goal = normalized
        self._sync_run_goal_from_market_stage(run, stage)
        self._require_market_research_goal(run, stage)

        chunk_specs = []
        for chunk in stage.chunks:
            if chunk.status == WizardChunkStatus.ACCEPTED:
                continue
            if chunk_id and chunk.id != chunk_id:
                continue
            chunk_specs.append({
                "id": chunk.id,
                "title": chunk.title,
                "guidance": chunk.guidance,
                "notes": chunk.notes_snapshot,
            })

        if not chunk_specs and chunk_id:
            raise ValueError("Selected chunk is already accepted.")
        if not chunk_specs:
            raise ValueError("All chunks in this stage are already accepted.")

        payload = self._executor.call_runtime(
            runtime=runtime,
            run=run,
            stage=stage,
            chunk_specs=chunk_specs,
            revision_note=revision_note,
            extra_context=extra_context,
        )

        self._merge_stage_payload(stage, payload)
        self._reveal_market_research_chunks(stage)
        self._sync_run_goal_from_market_stage(run, stage)
        if stage.status != WizardStageStatus.ACCEPTED:
            stage.status = WizardStageStatus.DRAFTED
        stage.updated_at = _now()
        self._ensure_stage_doc(run, stage)
        recompute_current_stage(run)
        self.save_run(run)
        return {
            "wizard": run.to_dict(),
            "openDocPath": stage.doc_path,
            "stageId": stage.id,
        }

    def accept_chunk(self, *, run_id: str, stage_id: str, chunk_id: str) -> dict:
        run = self.load_run(run_id)
        stage = self._require_stage(run, stage_id)
        chunk = stage.get_chunk(chunk_id)
        if not chunk:
            raise ValueError(f"Chunk not found: {chunk_id}")
        if not chunk.draft_text.strip():
            raise ValueError("Run the stage before accepting this chunk.")
        chunk.accepted_text = chunk.draft_text
        chunk.status = WizardChunkStatus.ACCEPTED
        chunk.updated_at = _now()
        stage.updated_at = _now()
        self._sync_run_goal_from_market_stage(run, stage)
        self._ensure_stage_doc(run, stage)
        recompute_current_stage(run)
        self.save_run(run)
        return {
            "wizard": run.to_dict(),
            "openDocPath": stage.doc_path,
            "stageId": stage.id,
            "chunkId": chunk.id,
        }

    def accept_stage(self, *, run_id: str, stage_id: str) -> dict:
        run = self.load_run(run_id)
        stage = self._require_stage(run, stage_id)
        if not is_stage_unlocked(run, stage):
            raise ValueError(f"Stage '{stage.title}' is locked until earlier stages are accepted.")

        if stage.id == MARKET_RESEARCH_TEMPLATE.id:
            for chunk in stage.chunks:
                if chunk.status == WizardChunkStatus.ACCEPTED:
                    continue
                if not chunk.draft_text.strip():
                    continue
                chunk.accepted_text = chunk.draft_text
                chunk.status = WizardChunkStatus.ACCEPTED
                chunk.updated_at = _now()

        missing = [chunk.title for chunk in stage.chunks if chunk.required and chunk.status != WizardChunkStatus.ACCEPTED]
        if missing:
            raise ValueError(f"Accept all required chunks first: {', '.join(missing)}")

        stage.status = WizardStageStatus.ACCEPTED
        stage.updated_at = _now()
        on_stage_accepted(run, stage, self._ensure_stage_doc, self._stage_from_template)
        self._ensure_stage_doc(run, stage)
        recompute_current_stage(run)
        self.save_run(run)
        open_stage = run.get_stage(run.current_stage_id) or stage
        self._ensure_stage_doc(run, open_stage)
        return {
            "wizard": run.to_dict(),
            "openDocPath": open_stage.doc_path,
            "stageId": stage.id,
        }

    def promote_todos(self, *, run_id: str, task_store: TaskStore) -> dict:
        run = self.load_run(run_id)
        promoted_ids: list[str] = []
        for stage in run.stages:
            if stage.status != WizardStageStatus.ACCEPTED:
                continue
            for todo in stage.todo_exports:
                if todo.promoted_task_id:
                    continue
                created = task_store.add_task(self._exporter.todo_to_task_input(stage, todo))
                todo.promoted_task_id = created.id
                run.derived_task_ids[todo.id] = created.id
                promoted_ids.append(created.id)
        self.save_run(run)
        open_stage = run.get_stage(run.current_stage_id) or run.stages[0]
        return {
            "wizard": run.to_dict(),
            "openDocPath": open_stage.doc_path,
            "createdTaskIds": promoted_ids,
            "count": len(promoted_ids),
        }

    def start_coding(
        self,
        *,
        run_id: str,
        session_manager: SessionManager,
        sessions: dict[str, PlanDocument],
        task_store: TaskStore,
    ) -> dict:
        run = self.load_run(run_id)
        coding_stage = self._require_stage(run, CODING_TEMPLATE.id)
        if coding_stage.status != WizardStageStatus.ACCEPTED:
            raise ValueError("Accept the Coding Agents stage before starting coding.")

        self.promote_todos(run_id=run_id, task_store=task_store)
        run = self.load_run(run_id)
        coding_stage = self._require_stage(run, CODING_TEMPLATE.id)
        session = self._load_or_create_linked_session(run, session_manager)
        session.tasks = self._exporter.build_session_tasks(run)
        session_manager.save_session(session)
        sessions[session.id] = session
        run.linked_session_id = session.id
        run.status = WizardRunStatus.CODING
        self.save_run(run)
        return {
            "wizard": run.to_dict(),
            "session": session.to_dict(),
            "openDocPath": coding_stage.doc_path,
        }

    def ensure_review_stage(self, run: WizardRun) -> WizardStageState:
        stage = run.get_stage(REVIEW_TEMPLATE.id)
        if stage:
            return stage
        stage = self._stage_from_template(run.id, REVIEW_TEMPLATE)
        run.stages.append(stage)
        self._ensure_stage_doc(run, stage)
        self.save_run(run)
        return stage

    def active_doc_path(self, run: WizardRun) -> str:
        stage = run.get_stage(run.current_stage_id) or run.stages[0]
        return stage.doc_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_latest_active_run(self) -> Optional[WizardRun]:
        if not self._wizard_root.exists():
            return None
        latest: Optional[WizardRun] = None
        for path in self._wizard_root.glob(f"*/{_WIZARD_FILENAME}"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                run = WizardRun.from_dict(payload)
            except Exception:
                continue
            if run.status == WizardRunStatus.COMPLETE:
                continue
            if latest is None or run.updated_at > latest.updated_at:
                latest = run
        return latest

    def _stage_from_template(self, run_id: str, template: StageTemplate, subsystem_name: str = "") -> WizardStageState:
        doc_path = self._doc_path_for_stage(run_id, template)
        return WizardStageState(
            id=template.id,
            kind=template.kind,
            title=template.title,
            persona=template.persona,
            doc_path=str(doc_path),
            subsystem_name=subsystem_name,
            chunks=[
                WizardChunkState(
                    id=chunk.id,
                    title=chunk.title,
                    required=chunk.required,
                    visible=template.id != MARKET_RESEARCH_TEMPLATE.id or index == 0,
                    guidance=chunk.guidance,
                )
                for index, chunk in enumerate(template.chunks)
            ],
        )

    def _doc_path_for_stage(self, run_id: str, template: StageTemplate) -> Path:
        if template.id == MARKET_RESEARCH_TEMPLATE.id:
            return self._public_docs_root() / f"market-research-{run_id[:8]}.md"
        return wizard_root(str(self._workspace), run_id) / template.relative_doc_path

    def _public_docs_root(self) -> Path:
        configured = Path(self._public_docs_path)
        if configured.is_absolute():
            return configured
        return (self._workspace / configured).resolve()

    def _require_stage(self, run: WizardRun, stage_id: str) -> WizardStageState:
        stage = run.get_stage(stage_id)
        if not stage:
            raise ValueError(f"Stage not found: {stage_id}")
        return stage

    def _ensure_static_stage(self, run: WizardRun, template: StageTemplate) -> WizardStageState:
        """Thin wrapper so tests that access this private method still work."""
        return ensure_static_stage(run, template, self._ensure_stage_doc, self._stage_from_template)

    def _ensure_stage_doc(self, run: WizardRun, stage: WizardStageState) -> None:
        notes_map = self._renderer.load_notes_map(stage)
        self._apply_notes_map(stage, notes_map)
        self._sync_run_goal_from_market_stage(run, stage)
        content = self._renderer.render_stage_doc(run, stage)
        doc_path = Path(stage.doc_path)
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(content, encoding="utf-8")

    def _migrate_market_research_doc(self, run: WizardRun) -> None:
        stage = run.get_stage(MARKET_RESEARCH_TEMPLATE.id)
        if not stage:
            return
        target_path = self._doc_path_for_stage(run.id, MARKET_RESEARCH_TEMPLATE)
        if Path(stage.doc_path).resolve() == target_path.resolve():
            return
        notes_map = self._renderer.load_notes_map(stage)
        self._apply_notes_map(stage, notes_map)
        stage.doc_path = str(target_path)

    def _apply_notes_map(self, stage: WizardStageState, notes_map: dict[str, str]) -> None:
        for chunk in stage.chunks:
            if chunk.id not in notes_map:
                continue
            chunk.notes_snapshot = self._sanitize_notes_snapshot(chunk, notes_map[chunk.id])

    def _sanitize_notes_snapshot(self, chunk: WizardChunkState, notes: str) -> str:
        candidate = notes.strip()
        if not candidate:
            return ""
        default_text = self._renderer._default_notes_text(chunk)
        fallback_text = f"Add notes for {chunk.title.lower()} here."
        if _normalize_goal_text(candidate) in {
            _normalize_goal_text(default_text),
            _normalize_goal_text(fallback_text),
        }:
            return ""
        return candidate

    def _sync_run_goal_from_market_stage(self, run: WizardRun, stage: WizardStageState) -> None:
        if stage.id != MARKET_RESEARCH_TEMPLATE.id:
            return
        idea_chunk = stage.get_chunk("initial_goal")
        if not idea_chunk:
            return
        for candidate in (idea_chunk.accepted_text, idea_chunk.notes_snapshot, idea_chunk.draft_text):
            normalized = _normalize_goal_text(candidate)
            if normalized:
                run.goal = normalized
                return

    def _require_market_research_goal(self, run: WizardRun, stage: WizardStageState) -> None:
        if stage.id != MARKET_RESEARCH_TEMPLATE.id:
            return
        if run.goal.strip():
            return
        raise ValueError("Describe the idea in 'What is the idea?' before running market research.")

    def _reveal_market_research_chunks(self, stage: WizardStageState) -> None:
        if stage.id != MARKET_RESEARCH_TEMPLATE.id:
            return
        if not any(chunk.draft_text.strip() or chunk.accepted_text.strip() for chunk in stage.chunks):
            return
        for chunk in stage.chunks:
            chunk.visible = True

    def _merge_stage_payload(self, stage: WizardStageState, payload: dict) -> None:
        chunk_payloads = {str(item.get("id", "")): item for item in payload.get("chunks", []) if str(item.get("id", "")).strip()}
        for chunk in stage.chunks:
            raw = chunk_payloads.get(chunk.id)
            if not raw or chunk.status == WizardChunkStatus.ACCEPTED:
                continue
            chunk.draft_text = str(raw.get("content", "")).strip()
            chunk.updated_at = _now()

        stage.summary = str(payload.get("stageSummary", "")).strip()
        stage.questions = [str(item).strip() for item in payload.get("questions", []) if str(item).strip()]
        stage.external_research_prompt = str(payload.get("externalResearchPrompt", "")).strip()

        if "subsystems" in payload:
            stage.derived_artifacts["subsystems"] = [
                str(item).strip()
                for item in payload.get("subsystems", [])
                if str(item).strip()
            ]
        stage.todo_exports = self._exporter.merge_todo_exports(stage, payload.get("todos", []))

    def _load_or_create_linked_session(self, run: WizardRun, session_manager: SessionManager) -> PlanDocument:
        current = session_manager.load_session()
        if run.linked_session_id and current and current.id == run.linked_session_id:
            return current
        return session_manager.create_session(run.goal, persona=run.persona)
