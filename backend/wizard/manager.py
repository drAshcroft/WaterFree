from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import json
import os
import re
import uuid

from backend.session.models import (
    CodeCoord,
    OwnerType,
    PlanDocument,
    Task,
    TaskOwner,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from backend.session.session_manager import SessionManager
from backend.todo.store import TaskStore
from backend.wizard.definitions import (
    ARCHITECT_TEMPLATE,
    BDD_TEMPLATE,
    CODING_TEMPLATE,
    MARKET_RESEARCH_TEMPLATE,
    REVIEW_TEMPLATE,
    StageTemplate,
    make_design_template,
    make_wireframe_template,
    wizard_root,
)
from backend.wizard.models import (
    WizardChunkState,
    WizardChunkStatus,
    WizardRun,
    WizardRunStatus,
    WizardStageState,
    WizardStageStatus,
    WizardTodoExport,
)

_WIZARD_FILENAME = "wizard.json"
_FRONTMATTER_BOUNDARY = "---"
_FRONTMATTER_RE = re.compile(r"^---\r?\n[\s\S]*?\r?\n---\r?\n?", re.DOTALL)
_INITIAL_MARKET_RESEARCH_TITLE = "What is your idea? (describe in detail)"


class WizardManager:
    def __init__(self, workspace_path: str, public_docs_path: str = "docs"):
        self._workspace = Path(workspace_path).resolve()
        self._wizard_root = self._workspace / ".waterfree" / "wizards"
        self._public_docs_path = public_docs_path.strip() or "docs"

    @property
    def root_path(self) -> Path:
        return self._wizard_root

    def set_public_docs_path(self, public_docs_path: str) -> None:
        self._public_docs_path = public_docs_path.strip() or "docs"

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
        if not self._is_stage_unlocked(run, stage):
            raise ValueError(f"Stage '{stage.title}' is locked until earlier stages are accepted.")

        notes_map = self._load_notes_map(stage)
        self._apply_notes_map(stage, notes_map)
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

        stage_context = self._build_stage_context(run, stage, extra_context=extra_context)
        run_stage_fn = getattr(runtime, "run_wizard_stage", None)
        payload = None
        if callable(run_stage_fn):
            payload = run_stage_fn(
                stage_kind=stage.kind,
                stage_title=stage.title,
                goal=run.goal,
                context=stage_context,
                chunk_specs=chunk_specs,
                workspace_path=run.workspace_path,
                persona=stage.persona,
                revision_request=revision_note,
                metadata={
                    "subsystemName": stage.subsystem_name,
                    "webToolsEnabled": bool(os.environ.get("WATERFREE_ENABLE_WEB_TOOLS", "").strip()),
                },
            )
        if not isinstance(payload, dict):
            payload = self._fallback_stage_payload(run, stage, chunk_specs, revision_note)

        self._merge_stage_payload(stage, payload)
        self._reveal_market_research_chunks(stage)
        self._sync_run_goal_from_market_stage(run, stage)
        if stage.status != WizardStageStatus.ACCEPTED:
            stage.status = WizardStageStatus.DRAFTED
        stage.updated_at = _now()
        self._ensure_stage_doc(run, stage)
        self._recompute_current_stage(run)
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
        self._recompute_current_stage(run)
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
        if not self._is_stage_unlocked(run, stage):
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
        self._on_stage_accepted(run, stage)
        self._ensure_stage_doc(run, stage)
        self._recompute_current_stage(run)
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
                created = task_store.add_task(self._todo_to_task_input(stage, todo))
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
        session.tasks = self._build_session_tasks(run)
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

    def _is_stage_unlocked(self, run: WizardRun, stage: WizardStageState) -> bool:
        stage_phase = _phase_for_stage(stage.id)
        for other in run.stages:
            if _phase_for_stage(other.id) < stage_phase and other.status != WizardStageStatus.ACCEPTED:
                return False
        return True

    def _recompute_current_stage(self, run: WizardRun) -> None:
        for stage in run.stages:
            if stage.status != WizardStageStatus.ACCEPTED and self._is_stage_unlocked(run, stage):
                run.current_stage_id = stage.id
                return
        run.current_stage_id = run.stages[-1].id if run.stages else ""

    def _on_stage_accepted(self, run: WizardRun, stage: WizardStageState) -> None:
        if stage.id == ARCHITECT_TEMPLATE.id:
            subsystems = [
                str(item).strip()
                for item in stage.derived_artifacts.get("subsystems", [])
                if str(item).strip()
            ]
            if not subsystems:
                subsystems = ["Core Application"]
            self._ensure_design_stages(run, subsystems)
            return

        if stage.id.startswith("design:") and self._all_accepted(run, prefix="design:"):
            subsystem_names = [candidate.subsystem_name for candidate in run.stages if candidate.id.startswith("design:")]
            self._ensure_wireframe_stages(run, subsystem_names)
            return

        if stage.id.startswith("wireframe:") and self._all_accepted(run, prefix="wireframe:"):
            self._ensure_static_stage(run, BDD_TEMPLATE)
            return

        if stage.id == BDD_TEMPLATE.id:
            self._ensure_static_stage(run, CODING_TEMPLATE)
            return

        if stage.id == CODING_TEMPLATE.id:
            self._ensure_static_stage(run, REVIEW_TEMPLATE)
            return

        if stage.id == REVIEW_TEMPLATE.id:
            run.status = WizardRunStatus.COMPLETE

    def _all_accepted(self, run: WizardRun, *, prefix: str) -> bool:
        relevant = [stage for stage in run.stages if stage.id.startswith(prefix)]
        return bool(relevant) and all(stage.status == WizardStageStatus.ACCEPTED for stage in relevant)

    def _ensure_design_stages(self, run: WizardRun, subsystems: list[str]) -> None:
        for subsystem_name in subsystems:
            template = make_design_template(subsystem_name)
            if run.get_stage(template.id):
                continue
            stage = self._stage_from_template(run.id, template, subsystem_name=subsystem_name)
            run.stages.append(stage)
            self._ensure_stage_doc(run, stage)

    def _ensure_wireframe_stages(self, run: WizardRun, subsystem_names: list[str]) -> None:
        for subsystem_name in subsystem_names:
            template = make_wireframe_template(subsystem_name)
            if run.get_stage(template.id):
                continue
            stage = self._stage_from_template(run.id, template, subsystem_name=subsystem_name)
            run.stages.append(stage)
            self._ensure_stage_doc(run, stage)

    def _ensure_static_stage(self, run: WizardRun, template: StageTemplate) -> WizardStageState:
        existing = run.get_stage(template.id)
        if existing:
            return existing
        stage = self._stage_from_template(run.id, template)
        run.stages.append(stage)
        self._ensure_stage_doc(run, stage)
        return stage

    def _ensure_stage_doc(self, run: WizardRun, stage: WizardStageState) -> None:
        notes_map = self._load_notes_map(stage)
        self._apply_notes_map(stage, notes_map)
        self._sync_run_goal_from_market_stage(run, stage)
        content = self._render_stage_doc(run, stage)
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
        notes_map = self._load_notes_map(stage)
        self._apply_notes_map(stage, notes_map)
        stage.doc_path = str(target_path)

    def _render_stage_doc(self, run: WizardRun, stage: WizardStageState) -> str:
        if stage.id == MARKET_RESEARCH_TEMPLATE.id:
            return self._render_market_research_doc(run, stage)

        visible_chunks = self._visible_chunks(stage)
        status_rows = "\n".join(
            f"| {chunk.title} | {self._chunk_label(chunk)} |"
            for chunk in visible_chunks
        )
        related = "\n".join(
            f"- `{self._related_doc_label(run, other)}`"
            for other in run.stages
            if Path(other.doc_path).exists()
        ) or "- `(current stage only)`"

        sections = [
            _render_frontmatter(
                {
                    "waterfreeWizard": "true",
                    "wizardId": run.wizard_id,
                    "runId": run.id,
                    "stageId": stage.id,
                    "stageKind": stage.kind,
                    "title": stage.title,
                }
            ),
            f"# {stage.title}",
            "",
            f"Goal: {run.goal.strip() or 'Describe the idea in the first chunk below.'}",
            "",
            f"Stage Status: {stage.status.value}",
            f"Persona: {stage.persona}",
            "",
            "## How To Use This Document",
            "",
            "- Edit the Working Notes blocks directly in this file.",
            "- Run the stage to refresh only the unaccepted chunks.",
            "- Accept Chunk freezes one chunk.",
            "- Accept Stage freezes the stage and unlocks the next phase once every required chunk is accepted.",
            "",
            "## Chunk Status",
            "",
            "| Chunk | Status |",
            "| --- | --- |",
            status_rows,
            "",
            "## Related Wizard Docs",
            "",
            related,
            "",
        ]

        if stage.summary.strip():
            sections.extend([
                "## Stage Summary",
                "",
                stage.summary.strip(),
                "",
            ])

        if stage.questions:
            sections.extend([
                "## Open Questions",
                "",
                *[f"- {question}" for question in stage.questions],
                "",
            ])

        if stage.external_research_prompt.strip():
            sections.extend([
                "## External Research Prompt",
                "",
                "Use this prompt in a web-capable tool if you want stronger market research.",
                "",
                stage.external_research_prompt.strip(),
                "",
            ])

        if stage.todo_exports:
            sections.extend([
                "## Todo Exports",
                "",
                "| Title | Type | Priority | Promoted |",
                "| --- | --- | --- | --- |",
            ])
            sections.extend(
                f"| {todo.title} | {todo.task_type.value} | {todo.priority.value} | {'yes' if todo.promoted_task_id else 'no'} |"
                for todo in stage.todo_exports
            )
            sections.append("")

        for chunk in visible_chunks:
            accepted = chunk.status == WizardChunkStatus.ACCEPTED
            sections.extend([
                f"## {chunk.title}",
                f"<!-- wf:chunk {json.dumps({'id': chunk.id, 'title': chunk.title, 'required': chunk.required, 'accepted': accepted})} -->",
                "",
                f"Status: {'Accepted' if accepted else 'Draft'}",
                "",
                "### Working Notes",
                f"<!-- wf:notes:{chunk.id}:start -->",
                chunk.notes_snapshot.strip() if chunk.notes_snapshot.strip() else self._default_notes_text(chunk),
                f"<!-- wf:notes:{chunk.id}:end -->",
                "",
                "### Latest Draft",
                f"<!-- wf:draft:{chunk.id}:start -->",
                chunk.draft_text.strip() if chunk.draft_text.strip() else "_Run the stage to generate this chunk._",
                f"<!-- wf:draft:{chunk.id}:end -->",
                "",
                "### Accepted Output",
                f"<!-- wf:accepted:{chunk.id}:start -->",
                chunk.accepted_text.strip() if chunk.accepted_text.strip() else "_Not accepted yet._",
                f"<!-- wf:accepted:{chunk.id}:end -->",
                "",
            ])

        return "\n".join(sections).rstrip() + "\n"

    def _render_market_research_doc(self, run: WizardRun, stage: WizardStageState) -> str:
        sections = [
            _render_frontmatter(
                {
                    "waterfreeWizard": "true",
                    "wizardId": run.wizard_id,
                    "runId": run.id,
                    "stageId": stage.id,
                    "stageKind": stage.kind,
                    "title": stage.title,
                }
            ),
        ]

        if self._is_initial_market_research_doc(stage):
            idea_chunk = stage.get_chunk("initial_goal")
            sections.extend([
                f"# {_INITIAL_MARKET_RESEARCH_TITLE}",
                "",
            ])
            if idea_chunk and idea_chunk.notes_snapshot.strip():
                sections.extend([
                    idea_chunk.notes_snapshot.strip(),
                    "",
                ])
            return "\n".join(sections).rstrip() + "\n"

        sections.extend([
            "# Market Research",
            "",
        ])

        for chunk in self._visible_chunks(stage):
            sections.extend([
                f"## {chunk.title}",
                "",
            ])
            content = self._market_research_chunk_content(chunk)
            if content:
                sections.extend([
                    content,
                    "",
                ])

        if stage.questions:
            sections.extend([
                "## Questions and Suggestions",
                "",
                *[f"- {question}" for question in stage.questions],
                "",
            ])

        if stage.external_research_prompt.strip():
            sections.extend([
                "## External Research Prompt",
                "",
                stage.external_research_prompt.strip(),
                "",
            ])

        return "\n".join(sections).rstrip() + "\n"

    def _visible_chunks(self, stage: WizardStageState) -> list[WizardChunkState]:
        return [chunk for chunk in stage.chunks if chunk.visible]

    def _default_notes_text(self, chunk: WizardChunkState) -> str:
        if chunk.guidance.strip():
            return chunk.guidance.strip()
        return f"Add notes for {chunk.title.lower()} here."

    def _apply_notes_map(self, stage: WizardStageState, notes_map: dict[str, str]) -> None:
        for chunk in stage.chunks:
            if chunk.id not in notes_map:
                continue
            chunk.notes_snapshot = self._sanitize_notes_snapshot(chunk, notes_map[chunk.id])

    def _sanitize_notes_snapshot(self, chunk: WizardChunkState, notes: str) -> str:
        candidate = notes.strip()
        if not candidate:
            return ""
        default_text = self._default_notes_text(chunk)
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

    def _load_notes_map(self, stage: WizardStageState) -> dict[str, str]:
        doc_path = Path(stage.doc_path)
        if not doc_path.exists():
            return {}
        content = doc_path.read_text(encoding="utf-8")
        if stage.id == MARKET_RESEARCH_TEMPLATE.id:
            return self._load_market_research_notes_map(stage, content)
        notes: dict[str, str] = {}
        for chunk in stage.chunks:
            pattern = re.compile(
                rf"<!-- wf:notes:{re.escape(chunk.id)}:start -->\s*(.*?)\s*<!-- wf:notes:{re.escape(chunk.id)}:end -->",
                re.DOTALL,
            )
            match = pattern.search(content)
            if match:
                notes[chunk.id] = match.group(1).strip()
        return notes

    def _load_market_research_notes_map(self, stage: WizardStageState, content: str) -> dict[str, str]:
        body = _strip_frontmatter(content).strip()
        if not body:
            return {}

        if "## " not in body:
            match = re.match(rf"^#\s+{re.escape(_INITIAL_MARKET_RESEARCH_TITLE)}\s*(.*)$", body, re.DOTALL)
            if not match:
                return {}
            idea_text = match.group(1).strip()
            return {"initial_goal": idea_text} if idea_text else {}

        title_to_chunk_id = {
            chunk.title.strip().lower(): chunk.id
            for chunk in stage.chunks
        }
        notes: dict[str, str] = {}
        section_re = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
        matches = list(section_re.finditer(body))
        for index, match in enumerate(matches):
            title = match.group(1).strip().lower()
            chunk_id = title_to_chunk_id.get(title)
            if not chunk_id:
                continue
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            section_body = body[start:end].strip()
            if section_body:
                notes[chunk_id] = section_body
        return notes

    def _is_initial_market_research_doc(self, stage: WizardStageState) -> bool:
        hidden_chunks = [chunk for chunk in stage.chunks if not chunk.visible]
        if hidden_chunks:
            return True
        if stage.summary.strip() or stage.questions or stage.external_research_prompt.strip():
            return False
        return not any(
            chunk.draft_text.strip() or chunk.accepted_text.strip()
            for chunk in stage.chunks
        )

    def _market_research_chunk_content(self, chunk: WizardChunkState) -> str:
        for candidate in (chunk.accepted_text, chunk.draft_text, chunk.notes_snapshot):
            if candidate.strip():
                return candidate.strip()
        return ""

    def _related_doc_label(self, run: WizardRun, stage: WizardStageState) -> str:
        doc_path = Path(stage.doc_path)
        for root in (wizard_root(run.workspace_path, run.id), Path(run.workspace_path)):
            try:
                return str(doc_path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
        return doc_path.name

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
        stage.todo_exports = self._merge_todo_exports(stage, payload.get("todos", []))

    def _merge_todo_exports(self, stage: WizardStageState, raw_todos: list[dict]) -> list[WizardTodoExport]:
        existing_by_key = {
            self._todo_identity(todo): todo
            for todo in stage.todo_exports
        }
        merged: list[WizardTodoExport] = []
        for raw in raw_todos:
            todo = WizardTodoExport(
                stage_id=stage.id,
                title=str(raw.get("title", "")).strip(),
                description=str(raw.get("description", "")).strip(),
                prompt=str(raw.get("prompt", "")).strip(),
                phase=str(raw.get("phase", "")).strip() or stage.title,
                priority=_coerce_priority(str(raw.get("priority", "P2"))),
                task_type=_coerce_task_type(str(raw.get("taskType", "impl"))),
                target_coord=CodeCoord(
                    file=str(raw.get("targetFile", "")).strip(),
                    method=str(raw.get("targetFunction", "")).strip() or None,
                ),
                owner_type=_coerce_owner_type(str(raw.get("ownerType", OwnerType.UNASSIGNED.value))),
                owner_name=str(raw.get("ownerName", "")).strip(),
            )
            if not todo.title:
                continue
            identity = self._todo_identity(todo)
            if identity in existing_by_key:
                todo.id = existing_by_key[identity].id
                todo.promoted_task_id = existing_by_key[identity].promoted_task_id
            merged.append(todo)
        return merged

    def _todo_identity(self, todo: WizardTodoExport) -> str:
        return "|".join([
            todo.title.strip().lower(),
            todo.description.strip().lower(),
            todo.task_type.value,
            todo.target_coord.file.strip().lower(),
            (todo.target_coord.method or "").strip().lower(),
        ])

    def _build_stage_context(self, run: WizardRun, stage: WizardStageState, *, extra_context: str = "") -> str:
        parts = [
            f"GOAL:\n{run.goal}",
            "",
            "ACCEPTED CONTEXT:",
        ]
        for other in run.stages:
            if _phase_for_stage(other.id) > _phase_for_stage(stage.id):
                continue
            accepted_chunks = [chunk for chunk in other.chunks if chunk.status == WizardChunkStatus.ACCEPTED]
            if not accepted_chunks:
                continue
            parts.append(f"## {other.title}")
            for chunk in accepted_chunks:
                parts.append(f"### {chunk.title}")
                parts.append(chunk.accepted_text.strip())
                parts.append("")
        if extra_context.strip():
            parts.extend(["EXTRA CONTEXT:", extra_context.strip(), ""])
        return "\n".join(parts).strip()

    def _fallback_stage_payload(
        self,
        run: WizardRun,
        stage: WizardStageState,
        chunk_specs: list[dict],
        revision_note: str,
    ) -> dict:
        chunks = []
        todos = []
        for spec in chunk_specs:
            note_text = str(spec.get("notes", "")).strip()
            body_lines = [
                f"{stage.title} draft for {run.goal}.",
                "",
                f"Focus: {spec['title']}.",
            ]
            if note_text:
                body_lines.extend(["", "Current notes:", note_text])
            if revision_note.strip():
                body_lines.extend(["", "Revision request:", revision_note.strip()])
            chunks.append({
                "id": spec["id"],
                "content": "\n".join(body_lines).strip(),
            })

        if stage.kind == "architect_review":
            todos.append({
                "title": "Architecture roadmap follow-up",
                "description": f"Turn the accepted architect outputs for '{run.goal}' into executable design work.",
                "priority": "P1",
                "taskType": "spike",
                "phase": stage.title,
            })
        elif stage.kind == "design_pattern_agent":
            todos.append({
                "title": f"Design subsystem: {stage.subsystem_name or stage.title}",
                "description": f"Refine interfaces and data flow for {stage.subsystem_name or stage.title}.",
                "priority": "P1",
                "taskType": "spike",
                "phase": stage.title,
            })
        elif stage.kind == "wireframe_agents":
            todos.append({
                "title": f"Wireframe subsystem: {stage.subsystem_name or stage.title}",
                "description": f"Create coding prompts and scaffolding guidance for {stage.subsystem_name or stage.title}.",
                "priority": "P1",
                "taskType": "impl",
                "phase": stage.title,
            })
        elif stage.kind == "bdd_ai_tests":
            todos.append({
                "title": "BDD acceptance coverage",
                "description": f"Write the accepted BDD scenarios for '{run.goal}'.",
                "priority": "P1",
                "taskType": "test",
                "phase": stage.title,
            })
        elif stage.kind == "coding_agents":
            todos.append({
                "title": "Implement accepted micro-prompts",
                "description": f"Build the accepted implementation tasks for '{run.goal}'.",
                "priority": "P1",
                "taskType": "impl",
                "phase": stage.title,
            })

        external_prompt = ""
        if stage.kind == "market_research":
            external_prompt = _external_market_research_prompt(run.goal)

        subsystems = []
        if stage.kind == "architect_review":
            subsystems = ["Core Application", "API Layer", "Data Layer"]

        return {
            "stageSummary": f"{stage.title} drafted for {run.goal}.",
            "chunks": chunks,
            "todos": todos,
            "subsystems": subsystems,
            "externalResearchPrompt": external_prompt,
            "questions": [],
        }

    def _todo_to_task_input(self, stage: WizardStageState, todo: WizardTodoExport) -> dict:
        return {
            "title": todo.title,
            "description": todo.description or todo.prompt or todo.title,
            "phase": todo.phase or stage.title,
            "priority": todo.priority.value,
            "taskType": todo.task_type.value,
            "owner": {
                "type": todo.owner_type.value,
                "name": todo.owner_name,
            },
            "targetCoord": todo.target_coord.to_dict(),
        }

    def _load_or_create_linked_session(self, run: WizardRun, session_manager: SessionManager) -> PlanDocument:
        current = session_manager.load_session()
        if run.linked_session_id and current and current.id == run.linked_session_id:
            return current
        return session_manager.create_session(run.goal, persona=run.persona)

    def _build_session_tasks(self, run: WizardRun) -> list[Task]:
        tasks: list[Task] = []
        for stage in run.stages:
            if stage.status != WizardStageStatus.ACCEPTED:
                continue
            for todo in stage.todo_exports:
                if todo.task_type == TaskType.SPIKE:
                    continue
                tasks.append(
                    Task(
                        title=todo.title,
                        description=todo.description or todo.prompt or todo.title,
                        priority=todo.priority,
                        phase=todo.phase or stage.title,
                        owner=TaskOwner(type=todo.owner_type, name=todo.owner_name),
                        task_type=todo.task_type,
                        target_coord=todo.target_coord,
                        status=TaskStatus.PENDING,
                    )
                )
        if tasks:
            return tasks
        return [
            Task(
                title=f"Implement {run.goal}",
                description="No todo exports were promoted, so coding starts from the accepted wizard summary.",
                priority=TaskPriority.P1,
                phase=CODING_TEMPLATE.title,
                owner=TaskOwner(type=OwnerType.UNASSIGNED, name=""),
                task_type=TaskType.IMPL,
                status=TaskStatus.PENDING,
            )
        ]

    def _chunk_label(self, chunk: WizardChunkState) -> str:
        return "Accepted" if chunk.status == WizardChunkStatus.ACCEPTED else "Draft"


def _phase_for_stage(stage_id: str) -> int:
    if stage_id == MARKET_RESEARCH_TEMPLATE.id:
        return 1
    if stage_id == ARCHITECT_TEMPLATE.id:
        return 2
    if stage_id.startswith("design:"):
        return 3
    if stage_id.startswith("wireframe:"):
        return 4
    if stage_id == BDD_TEMPLATE.id:
        return 5
    if stage_id == CODING_TEMPLATE.id:
        return 6
    if stage_id == REVIEW_TEMPLATE.id:
        return 7
    return 99


def _coerce_priority(raw: str) -> TaskPriority:
    try:
        return TaskPriority(raw)
    except Exception:
        return TaskPriority.P2


def _coerce_task_type(raw: str) -> TaskType:
    try:
        return TaskType(raw)
    except Exception:
        return TaskType.IMPL


def _coerce_owner_type(raw: str) -> OwnerType:
    try:
        return OwnerType(raw)
    except Exception:
        return OwnerType.UNASSIGNED


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_goal_text(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip()


def _render_frontmatter(values: dict[str, str]) -> str:
    lines = [_FRONTMATTER_BOUNDARY]
    for key, value in values.items():
        lines.append(f"{key}: {value}")
    lines.append(_FRONTMATTER_BOUNDARY)
    return "\n".join(lines)


def _strip_frontmatter(content: str) -> str:
    return _FRONTMATTER_RE.sub("", content, count=1)


def _external_market_research_prompt(goal: str) -> str:
    return (
        "Research this software idea on the live web and return a concise market memo.\n\n"
        f"Idea: {goal}\n\n"
        "Populate these sections:\n"
        "- Similar Ideas and Niches: comparable products, adjacent niches, what looks strong, weak, or differentiated\n"
        "- Who Wants This?: likely audiences, pains, urgency, and why they would care\n"
        "\nAlso cover:\n"
        "- realistic MVP\n"
        "- pricing or monetization signals if visible\n"
        "- risks or reasons the idea may fail\n"
    )
