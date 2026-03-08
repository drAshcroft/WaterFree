"""
Durable checkpoint storage under workspace-local .waterfree/checkpoints.db.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CheckpointStore:
    def __init__(self, workspace_path: str):
        workspace = Path(workspace_path).resolve()
        db_dir = workspace / ".waterfree"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "checkpoints.db"
        self.path = str(db_path)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    runtime_id TEXT NOT NULL,
                    subagent_id TEXT,
                    requires_approval INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    touched_files TEXT NOT NULL DEFAULT '[]',
                    tool_calls TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    decision TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create_checkpoint(
        self,
        *,
        session_id: str,
        reason: str,
        runtime_id: str,
        payload: dict[str, Any],
        subagent_id: str = "",
        requires_approval: bool = True,
        summary: str = "",
        touched_files: Optional[list[str]] = None,
        tool_calls: Optional[list[dict[str, str]]] = None,
    ) -> dict[str, Any]:
        checkpoint_id = payload.get("id") or str(uuid.uuid4())
        created_at = str(payload.get("createdAt") or _utcnow())
        touched = list(touched_files or payload.get("touchedFiles", []))
        calls = list(tool_calls or payload.get("toolCalls", []))
        checkpoint_summary = summary or str(payload.get("summary", "") or reason)
        serialized_payload = dict(payload)
        serialized_payload.setdefault("summary", checkpoint_summary)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO checkpoints (
                    id, session_id, reason, created_at, runtime_id, subagent_id,
                    requires_approval, summary, touched_files, tool_calls, payload,
                    status, decision, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '{}', ?)
                """,
                (
                    checkpoint_id,
                    session_id,
                    reason,
                    created_at,
                    runtime_id,
                    subagent_id or None,
                    1 if requires_approval else 0,
                    checkpoint_summary,
                    json.dumps(touched),
                    json.dumps(calls),
                    json.dumps(serialized_payload),
                    created_at,
                ),
            )
        checkpoint = self.get_checkpoint(checkpoint_id)
        if checkpoint is None:  # pragma: no cover - defensive
            raise RuntimeError("Checkpoint creation failed.")
        return checkpoint

    def list_checkpoints(self, session_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            if session_id:
                rows = self._conn.execute(
                    "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY created_at DESC",
                    (session_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM checkpoints ORDER BY created_at DESC"
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_checkpoint(self, checkpoint_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM checkpoints WHERE id = ?",
                (checkpoint_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def resume_checkpoint(self, checkpoint_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        current = self.get_checkpoint(checkpoint_id)
        if current is None:
            raise ValueError(f"Checkpoint not found: {checkpoint_id}")
        status = str(decision.get("action", "")).lower() or "resumed"
        if status not in {"approve", "approved", "resume", "resumed"}:
            status = "resumed"
        updated_at = _utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE checkpoints
                SET status = ?, decision = ?, updated_at = ?
                WHERE id = ?
                """,
                ("approved" if status.startswith("approve") else "resumed", json.dumps(decision), updated_at, checkpoint_id),
            )
        checkpoint = self.get_checkpoint(checkpoint_id)
        if checkpoint is None:  # pragma: no cover - defensive
            raise RuntimeError("Checkpoint update failed.")
        return checkpoint

    def discard_checkpoint(self, checkpoint_id: str) -> bool:
        if self.get_checkpoint(checkpoint_id) is None:
            return False
        updated_at = _utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE checkpoints SET status = 'discarded', updated_at = ? WHERE id = ?",
                (updated_at, checkpoint_id),
            )
        return True

    def close(self) -> None:
        self._conn.close()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "sessionId": row["session_id"],
            "reason": row["reason"],
            "createdAt": row["created_at"],
            "runtimeId": row["runtime_id"],
            "subagentId": row["subagent_id"],
            "requiresApproval": bool(row["requires_approval"]),
            "summary": row["summary"],
            "touchedFiles": json.loads(row["touched_files"] or "[]"),
            "toolCalls": json.loads(row["tool_calls"] or "[]"),
            "payload": json.loads(row["payload"] or "{}"),
            "status": row["status"],
            "decision": json.loads(row["decision"] or "{}"),
            "updatedAt": row["updated_at"],
        }
