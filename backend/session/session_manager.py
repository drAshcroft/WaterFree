"""
SessionManager — persists and loads PlanDocument sessions.
Sessions are stored as JSON under <workspace>/.waterfree/sessions/
"""

from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.session.models import PlanDocument, SessionStatus, AIState

log = logging.getLogger(__name__)

PAIRS_DIR = ".waterfree"
SESSIONS_DIR = ".waterfree/sessions"
CURRENT_FILE = "current.json"
ARCHIVE_DIR = "archive"
PLAN_SUMMARY_FILE = "plan.md"


class SessionManager:
    def __init__(self, workspace_path: str):
        self._workspace = Path(workspace_path).resolve()
        self._pairs_dir = self._workspace / PAIRS_DIR
        self._sessions_dir = self._workspace / SESSIONS_DIR
        self._current_path = self._sessions_dir / CURRENT_FILE
        self._archive_dir = self._sessions_dir / ARCHIVE_DIR
        self._plan_summary_path = self._pairs_dir / PLAN_SUMMARY_FILE

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(self, goal: str, persona: str = "default") -> PlanDocument:
        now = _now()
        doc = PlanDocument(
            goal_statement=goal,
            workspace_path=str(self._workspace),
            persona=persona,
            status=SessionStatus.PLANNING,
            ai_state=AIState.PLANNING,
            created_at=now,
            updated_at=now,
        )
        self.save_session(doc)
        return doc

    def save_session(self, doc: PlanDocument) -> None:
        doc.updated_at = _now()
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._current_path.write_text(json.dumps(doc.to_dict(), indent=2))
        self._write_plan_summary(doc)
        log.debug("Session saved: %s", doc.id)

    def load_session(self) -> Optional[PlanDocument]:
        """Load the current session, or None if none exists."""
        if not self._current_path.exists():
            return None
        try:
            data = json.loads(self._current_path.read_text())
            doc = PlanDocument.from_dict(data)
            log.info("Loaded session: %s — %s", doc.id, doc.goal_statement[:60])
            return doc
        except Exception as e:
            log.warning("Failed to load session: %s", e)
            return None

    def _write_plan_summary(self, doc: PlanDocument) -> None:
        """Write a human-readable .waterfree/plan.md so the editor shows current plan on open."""
        try:
            self._pairs_dir.mkdir(parents=True, exist_ok=True)
            lines = [
                f"# {doc.goal_statement}\n",
                f"**Status**: {doc.status}  ",
                f"**Updated**: {doc.updated_at}\n",
                "## Tasks\n",
            ]
            for task in doc.tasks:
                check = "x" if task.status == "complete" else " "
                lines.append(f"- [{check}] **{task.title}**")
                if task.description and task.description != task.title:
                    lines.append(f"  > {task.description}")
                if task.target_file:
                    rel = task.target_file
                    try:
                        rel = str(Path(task.target_file).relative_to(self._workspace))
                    except ValueError:
                        pass
                    lines.append(f"  `{rel}`")
            if doc.notes:
                lines.append("\n## Notes\n")
                for note in doc.notes[-5:]:
                    lines.append(f"- [{note.author}] {note.text}")
            self._plan_summary_path.write_text("\n".join(lines) + "\n")
        except Exception as e:
            log.debug("Could not write plan.md: %s", e)

    def archive_session(self, doc: PlanDocument) -> None:
        """Move the current session to the archive."""
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        slug = _slug(doc.goal_statement)
        date_str = doc.created_at[:10] if doc.created_at else _now()[:10]
        archive_path = self._archive_dir / f"{date_str}-{slug}.json"
        # avoid name collision
        counter = 1
        while archive_path.exists():
            archive_path = self._archive_dir / f"{date_str}-{slug}-{counter}.json"
            counter += 1
        doc.status = SessionStatus.COMPLETE
        doc.updated_at = _now()
        archive_path.write_text(json.dumps(doc.to_dict(), indent=2))
        if self._current_path.exists():
            self._current_path.unlink()
        log.info("Session archived to %s", archive_path)

    def discard_current(self) -> None:
        """Delete the current session without archiving."""
        if self._current_path.exists():
            self._current_path.unlink()

    def list_archived(self) -> list[dict]:
        """Return metadata for all archived sessions."""
        if not self._archive_dir.exists():
            return []
        result = []
        for p in sorted(self._archive_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text())
                result.append({
                    "id": data.get("id"),
                    "goalStatement": data.get("goalStatement", ""),
                    "status": data.get("status", ""),
                    "createdAt": data.get("createdAt", ""),
                    "file": p.name,
                })
            except Exception:
                pass
        return result


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    """Convert a goal statement to a safe filename slug."""
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower())
    return slug[:40].strip("-")
