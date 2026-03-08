"""
LLM-based knowledge extractor — two-pass design.

Pass 1 — TRIAGE
  Sends only symbol names, labels, and first-line signatures (no full code)
  in large batches (up to 50). The LLM reads the index and selects which
  symbols are worth examining further. Cheap and fast.

Pass 2 — DESCRIBE
  Fetches full code only for the symbols selected in Pass 1 (typically 20-40%).
  The LLM writes a title, description, snippet_type, and tags for each keeper.

A user-supplied `focus` string is injected into both system prompts so the LLM
can filter toward what the user actually cares about (e.g. "authentication
patterns", "error handling", "Django ORM usage").
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.store import KnowledgeStore
from backend.llm.runtime import AgentRuntime

log = logging.getLogger(__name__)

_TRIAGE_BATCH = 50    # how many signatures to show per triage call
_DESCRIBE_BATCH = 12  # how many full-code snippets per describe call
_MIN_BODY_CHARS = 30  # ignore trivially short snippets


class KnowledgeExtractor:
    """Two-pass LLM-based knowledge extractor."""

    def __init__(
        self,
        store: KnowledgeStore,
        runtime: AgentRuntime,
        source_repo: str,
        source_repo_url: str = "",
        focus: str = "",
        workspace_path: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ):
        self._store = store
        self._runtime = runtime
        self._source_repo = source_repo
        self._source_repo_url = source_repo_url
        self._focus = focus.strip()
        self._workspace_path = workspace_path
        self._progress_cb = progress_cb

    # ── Public API ──────────────────────────────────────────────────────────

    def extract_from_symbols(self, symbols: list[dict]) -> int:
        """
        Run two-pass extraction over a list of symbol dicts.
        Each dict needs: name, label, file_path, body (or source).
        Returns the number of new entries added to the store.
        """
        eligible = [
            s for s in symbols
            if len((s.get("body") or s.get("source") or "").strip()) >= _MIN_BODY_CHARS
        ]

        if not eligible:
            return 0

        total = len(eligible)
        log.info("extractor: pass 1 triage — %d eligible symbols", total)

        # Pass 1: triage in large batches (names + signatures only)
        selected_indices: list[int] = []
        for batch_start in range(0, total, _TRIAGE_BATCH):
            batch = eligible[batch_start : batch_start + _TRIAGE_BATCH]
            try:
                picked = self._triage_batch(batch, batch_start)
                selected_indices.extend(picked)
            except Exception as exc:
                log.warning("extractor: triage batch %d failed: %s", batch_start, exc)

            if self._progress_cb:
                # Report triage progress as first half of the total
                done = min(batch_start + _TRIAGE_BATCH, total)
                self._progress_cb(done // 2, total)

            if batch_start + _TRIAGE_BATCH < total:
                time.sleep(0.3)

        log.info("extractor: pass 1 selected %d/%d symbols", len(selected_indices), total)

        # Pass 2: describe selected symbols with full code
        selected_symbols = [eligible[i] for i in selected_indices if i < len(eligible)]
        added = 0

        for batch_start in range(0, len(selected_symbols), _DESCRIBE_BATCH):
            batch = selected_symbols[batch_start : batch_start + _DESCRIBE_BATCH]
            try:
                new = self._describe_batch(batch)
                added += new
            except Exception as exc:
                log.warning("extractor: describe batch %d failed: %s", batch_start, exc)

            if self._progress_cb:
                # Report describe progress as second half
                done = total // 2 + (batch_start + _DESCRIBE_BATCH)
                self._progress_cb(min(done, total), total)

            if batch_start + _DESCRIBE_BATCH < len(selected_symbols):
                time.sleep(0.5)

        log.info(
            "extractor: %d/%d selected described, %d entries added",
            len(selected_symbols), total, added,
        )
        return added

    # ── Pass 1: triage ──────────────────────────────────────────────────────

    def _triage_batch(self, batch: list[dict], global_offset: int) -> list[int]:
        """Send signatures to the LLM; return absolute indices of selected symbols."""
        local_indices = self._runtime.triage_knowledge_symbols(
            source_repo=self._source_repo,
            focus=self._focus,
            batch=batch,
            workspace_path=self._workspace_path,
        )
        # Convert batch-local indices to absolute indices into `eligible`
        return [global_offset + i for i in local_indices if 0 <= i < len(batch)]

    # ── Pass 2: describe ────────────────────────────────────────────────────

    def _describe_batch(self, batch: list[dict]) -> int:
        """Send full code for selected symbols; store accepted entries. Returns count added."""
        entries_data = self._runtime.describe_knowledge_batch(
            source_repo=self._source_repo,
            focus=self._focus,
            batch=batch,
            workspace_path=self._workspace_path,
        )

        added = 0
        for item in entries_data:
            if not item.get("keep"):
                continue
            idx = item.get("index", -1)
            if idx < 0 or idx >= len(batch):
                continue

            symbol = batch[idx]
            body = (symbol.get("body") or symbol.get("source") or "").strip()
            if not body:
                continue

            entry = KnowledgeEntry.create(
                source_repo=self._source_repo,
                source_file=symbol.get("file_path", ""),
                snippet_type=item.get("snippet_type", "pattern"),
                title=item.get("title", symbol.get("name", "Untitled")),
                description=item.get("description", ""),
                code=body,
                tags=item.get("tags", []),
                source_repo_url=self._source_repo_url,
            )

            if self._store.add_entry(entry):
                added += 1

        return added
