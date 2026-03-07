"""
Context lifecycle management for long-running pair sessions.

Tracks chunk utility across turns, compresses low-value chunks, and enforces
a stage-specific context budget.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")
_DEFAULT_BUDGETS = {
    "planning": 7000,
    "annotation": 9000,
    "execution": 15000,
    "question": 3000,
}
_MIN_SCORE_TO_STAY_ACTIVE = 0.35
_COMPRESS_AFTER_STREAK = 2
_DROP_AFTER_STREAK = 5


@dataclass
class ContextGovernanceResult:
    context: str
    stats: dict[str, Any]


class ContextLifecycleManager:
    def govern(
        self,
        workspace_path: str,
        session_id: str,
        stage: str,
        query: str,
        raw_context: str,
        budget_chars: int | None = None,
    ) -> ContextGovernanceResult:
        state_path = self._state_path(workspace_path)
        state = self._load_state(state_path)
        session = state.setdefault("sessions", {}).setdefault(
            session_id,
            {"turn": 0, "chunks": {}},
        )
        session["turn"] = int(session.get("turn", 0)) + 1
        turn = session["turn"]
        chunks_state: dict[str, dict[str, Any]] = session["chunks"]

        chunks = self._split_chunks(raw_context)
        if not chunks:
            return ContextGovernanceResult(context=raw_context, stats={"chunks": 0, "selected": 0})

        query_tokens = self._tokens(query)
        candidates: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for chunk_text in chunks:
            chunk_id = self._chunk_id(stage, chunk_text)
            seen_ids.add(chunk_id)
            rec = chunks_state.get(chunk_id, self._new_chunk_record(chunk_id, stage, turn, chunk_text))
            rec["text"] = chunk_text
            rec["stage"] = stage
            rec["last_seen_turn"] = turn

            relevance = self._relevance(query_tokens, self._tokens(chunk_text))
            task_link = 1.0 if relevance >= 0.4 else 0.0
            age = max(0, turn - int(rec.get("last_used_turn", turn)))
            recency = math.exp(-age / 5.0)
            freshness = 1.0
            reuse_count = int(rec.get("reuse_count", 0))
            reuse_norm = min(1.0, reuse_count / 5.0)

            score = (
                0.40 * relevance
                + 0.20 * task_link
                + 0.15 * recency
                + 0.15 * freshness
                + 0.10 * reuse_norm
            )
            rec["score"] = round(score, 4)

            if score < _MIN_SCORE_TO_STAY_ACTIVE:
                rec["low_score_streak"] = int(rec.get("low_score_streak", 0)) + 1
            else:
                rec["low_score_streak"] = 0

            if int(rec["low_score_streak"]) >= _DROP_AFTER_STREAK:
                rec["state"] = "dropped"
            elif int(rec["low_score_streak"]) >= _COMPRESS_AFTER_STREAK and len(chunk_text) > 220:
                rec["state"] = "compressed"
                rec["summary"] = self._summarize(chunk_text)
            else:
                rec["state"] = "active"

            chunks_state[chunk_id] = rec
            candidates.append(rec)

        # Keep unseen records for history; they simply won't be selected this turn.
        budget = budget_chars or _DEFAULT_BUDGETS.get(stage, 7000)
        selected = self._select_chunks(candidates, budget, turn)
        output = "\n\n".join(selected).strip()
        if not output:
            output = raw_context

        self._save_state(state_path, state)
        stats = {
            "chunks": len(candidates),
            "selected": len(selected),
            "active": sum(1 for c in candidates if c.get("state") == "active"),
            "compressed": sum(1 for c in candidates if c.get("state") == "compressed"),
            "dropped": sum(1 for c in candidates if c.get("state") == "dropped"),
            "budgetChars": budget,
        }
        return ContextGovernanceResult(context=output, stats=stats)

    def inspect(self, workspace_path: str) -> dict[str, Any]:
        state = self._load_state(self._state_path(workspace_path))
        sessions = state.get("sessions", {})
        return {
            "sessions": list(sessions.keys()),
            "sessionCount": len(sessions),
            "statePath": str(self._state_path(workspace_path)),
        }

    def reset(self, workspace_path: str, session_id: str | None = None) -> dict[str, Any]:
        path = self._state_path(workspace_path)
        state = self._load_state(path)
        sessions = state.setdefault("sessions", {})
        if session_id:
            existed = session_id in sessions
            sessions.pop(session_id, None)
            self._save_state(path, state)
            return {"ok": True, "removedSession": session_id, "existed": existed}
        if path.exists():
            path.unlink()
        return {"ok": True, "removedAll": True}

    def _select_chunks(self, candidates: list[dict[str, Any]], budget: int, turn: int) -> list[str]:
        ranked = sorted(
            candidates,
            key=lambda c: (
                0 if c.get("state") == "active" else 1,
                -float(c.get("score", 0.0)),
                c.get("id", ""),
            ),
        )
        selected: list[str] = []
        used = 0
        for rec in ranked:
            if rec.get("state") == "dropped":
                continue
            if rec.get("state") == "compressed":
                text = f"[COMPRESSED CONTEXT]\n{rec.get('summary', '')}"
            else:
                text = str(rec.get("text", ""))
            if not text:
                continue
            delta = len(text) + (2 if selected else 0)
            if selected and (used + delta) > budget:
                continue
            if not selected and delta > budget:
                selected.append(text[:budget])
                rec["reuse_count"] = int(rec.get("reuse_count", 0)) + 1
                rec["last_used_turn"] = turn
                break
            selected.append(text)
            used += delta
            rec["reuse_count"] = int(rec.get("reuse_count", 0)) + 1
            rec["last_used_turn"] = turn
        return selected

    def _state_path(self, workspace_path: str) -> Path:
        return Path(workspace_path) / ".waterfree" / "context_lifecycle.json"

    def _load_state(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            return {"sessions": {}}
        except json.JSONDecodeError:
            log.warning("context lifecycle state is invalid JSON: %s", path)
            return {"sessions": {}}

    def _save_state(self, path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _split_chunks(self, text: str) -> list[str]:
        chunks = [part.strip() for part in text.split("\n\n") if part.strip()]
        return chunks

    def _chunk_id(self, stage: str, text: str) -> str:
        digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return f"{stage}:{digest}"

    def _new_chunk_record(self, chunk_id: str, stage: str, turn: int, text: str) -> dict[str, Any]:
        return {
            "id": chunk_id,
            "stage": stage,
            "state": "active",
            "text": text,
            "summary": "",
            "score": 0.0,
            "low_score_streak": 0,
            "reuse_count": 0,
            "last_seen_turn": turn,
            "last_used_turn": turn,
        }

    def _tokens(self, text: str) -> set[str]:
        return {t.lower() for t in _TOKEN_RE.findall(text)}

    def _relevance(self, a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        if inter == 0:
            return 0.0
        return inter / float(len(a | b))

    def _summarize(self, text: str) -> str:
        compact = " ".join(text.split())
        if len(compact) <= 180:
            return compact
        return compact[:180].rstrip() + "..."
