from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from backend.wizard.definitions import MARKET_RESEARCH_TEMPLATE
from backend.wizard.models import WizardChunkState, WizardChunkStatus, WizardStageState, WizardRun

if TYPE_CHECKING:
    pass

_FRONTMATTER_BOUNDARY = "---"
_FRONTMATTER_RE = re.compile(r"^---\r?\n[\s\S]*?\r?\n---\r?\n?", re.DOTALL)
_INITIAL_MARKET_RESEARCH_TITLE = "What is your idea? (describe in detail)"


def render_frontmatter(values: dict[str, str]) -> str:
    lines = [_FRONTMATTER_BOUNDARY]
    for key, value in values.items():
        lines.append(f"{key}: {value}")
    lines.append(_FRONTMATTER_BOUNDARY)
    return "\n".join(lines)


def strip_frontmatter(content: str) -> str:
    return _FRONTMATTER_RE.sub("", content, count=1)


class DocumentRenderer:
    """Pure rendering functions that take run/stage data and return markdown strings."""

    def render_stage_doc(self, run: WizardRun, stage: WizardStageState) -> str:
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
            render_frontmatter(
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
            render_frontmatter(
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

    def _chunk_label(self, chunk: WizardChunkState) -> str:
        return "Accepted" if chunk.status == WizardChunkStatus.ACCEPTED else "Draft"

    def _market_research_chunk_content(self, chunk: WizardChunkState) -> str:
        for candidate in (chunk.accepted_text, chunk.draft_text, chunk.notes_snapshot):
            if candidate.strip():
                return candidate.strip()
        return ""

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

    def _default_notes_text(self, chunk: WizardChunkState) -> str:
        if chunk.guidance.strip():
            return chunk.guidance.strip()
        return f"Add notes for {chunk.title.lower()} here."

    def _related_doc_label(self, run: WizardRun, stage: WizardStageState) -> str:
        from backend.wizard.definitions import wizard_root
        doc_path = Path(stage.doc_path)
        for root in (wizard_root(run.workspace_path, run.id), Path(run.workspace_path)):
            try:
                return str(doc_path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
        return doc_path.name

    # ------------------------------------------------------------------
    # Notes extraction from on-disk documents
    # ------------------------------------------------------------------

    def load_notes_map(self, stage: WizardStageState) -> dict[str, str]:
        """Read the on-disk document for a stage and extract the current notes per chunk."""
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
        body = _FRONTMATTER_RE.sub("", content, count=1).strip()
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
