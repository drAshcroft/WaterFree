"""
Anthropic runtime adapter.

This keeps current Claude behavior but exposes it behind the shared runtime
boundary for future provider/runtime routing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from backend.llm.claude_client import ClaudeClient
from backend.llm.checkpoints.store import CheckpointStore


class AnthropicRuntime(ClaudeClient):
    def __init__(
        self,
        *args,
        checkpoint_store_factory: Optional[Callable[[str], CheckpointStore]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._checkpoint_store_factory = checkpoint_store_factory or (lambda workspace: CheckpointStore(workspace))
        self._checkpoint_stores: dict[str, CheckpointStore] = {}

    @property
    def runtime_id(self) -> str:
        return "anthropic"

    def checkpoint(self, session_id: str, reason: str, payload: dict) -> dict:
        workspace_path = str(payload.get("workspacePath", "") or ".")
        store = self._checkpoint_store(workspace_path)
        return store.create_checkpoint(
            session_id=session_id,
            reason=reason,
            runtime_id=self.runtime_id,
            payload=payload,
            subagent_id=str(payload.get("subagentId", "")),
            requires_approval=bool(payload.get("requiresApproval", True)),
            summary=str(payload.get("summary", "")).strip(),
            touched_files=list(payload.get("touchedFiles", [])),
            tool_calls=list(payload.get("toolCalls", [])),
        )

    def resume(self, checkpoint_id: str, decision: dict) -> dict:
        store = self._resolve_checkpoint_store(checkpoint_id, decision)
        return store.resume_checkpoint(checkpoint_id, decision)

    def discard(self, checkpoint_id: str, decision: Optional[dict[str, Any]] = None) -> dict:
        store = self._resolve_checkpoint_store(checkpoint_id, decision or {})
        ok = store.discard_checkpoint(checkpoint_id)
        return {"ok": ok, "checkpointId": checkpoint_id}

    def list_checkpoints(self, workspace_path: str, session_id: str = "") -> list[dict]:
        store = self._checkpoint_store(workspace_path)
        return store.list_checkpoints(session_id=session_id)

    def _checkpoint_store(self, workspace_path: str) -> CheckpointStore:
        resolved = str(Path(workspace_path).resolve())
        if resolved not in self._checkpoint_stores:
            self._checkpoint_stores[resolved] = self._checkpoint_store_factory(resolved)
        return self._checkpoint_stores[resolved]

    def _resolve_checkpoint_store(self, checkpoint_id: str, decision: dict[str, Any]) -> CheckpointStore:
        workspace_hint = str(decision.get("workspacePath", "")).strip()
        if workspace_hint:
            store = self._checkpoint_store(workspace_hint)
            if store.get_checkpoint(checkpoint_id):
                return store
        for store in self._checkpoint_stores.values():
            if store.get_checkpoint(checkpoint_id):
                return store
        raise ValueError(f"Checkpoint not found: {checkpoint_id}")
