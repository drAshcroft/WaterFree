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

import json
import logging
import os
import time
from typing import Callable, Optional

import anthropic

from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.store import KnowledgeStore

log = logging.getLogger(__name__)

_TRIAGE_BATCH = 50    # how many signatures to show per triage call
_DESCRIBE_BATCH = 12  # how many full-code snippets per describe call
_MAX_BODY_CHARS = 900 # truncate very long bodies before sending
_MIN_BODY_CHARS = 30  # ignore trivially short snippets

# ── Tool schemas ────────────────────────────────────────────────────────────

_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "0-based indices of symbols worth fetching full code for. "
                "Be selective — pick only those that look genuinely reusable."
            ),
        }
    },
    "required": ["selected"],
}

_DESCRIBE_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "0-based index in the input batch",
                    },
                    "keep": {
                        "type": "boolean",
                        "description": "true if this snippet is worth storing",
                    },
                    "snippet_type": {
                        "type": "string",
                        "enum": ["pattern", "utility", "style", "api_usage", "convention"],
                    },
                    "title": {
                        "type": "string",
                        "description": "Short, searchable title (max 10 words)",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "2-sentence plain-English description of what it does "
                            "and why it is reusable across projects"
                        ),
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-6 lowercase tags: language, framework, domain, concept",
                    },
                },
                "required": ["index", "keep"],
            },
        }
    },
    "required": ["entries"],
}

# ── System prompts ───────────────────────────────────────────────────────────

_TRIAGE_SYSTEM = """\
You are a code knowledge curator. You will receive a list of symbol names and \
signatures from a codebase. Your job is to identify which ones look worth \
fetching full source code for, based on whether they might contain reusable \
patterns, utilities, conventions, or non-obvious API usage.

{focus_clause}

Look for:
- Utility helpers useful beyond this project
- Interesting algorithmic patterns
- Non-trivial API integrations
- Architectural patterns or conventions

Skip obvious candidates:
- CRUD operations on project-specific models
- Auto-generated or migration code
- Trivial getters/setters/property accessors
- Test fixtures with no transferable technique

Be selective — aim to select 15-30% of symbols for deeper inspection.\
"""

_DESCRIBE_SYSTEM = """\
You are a code knowledge curator. You will receive full source code for a set \
of pre-selected symbols from the '{repo}' project. For each one, decide whether \
it is genuinely worth storing in a cross-project knowledge base, and if so \
write a title, description, snippet_type, and tags.

{focus_clause}

Keep a snippet ONLY if it demonstrates a reusable pattern, utility, convention, \
or non-obvious technique. Write descriptions in plain English that will make \
this snippet retrievable via keyword search. Be specific: say what the function \
does and what makes it reusable, not just "this is a utility function".\
"""

_FOCUS_CLAUSE = "The user specifically wants knowledge about: {focus}"
_NO_FOCUS_CLAUSE = "Apply general judgement — keep broadly reusable content."


class KnowledgeExtractor:
    """Two-pass LLM-based knowledge extractor."""

    def __init__(
        self,
        store: KnowledgeStore,
        source_repo: str,
        source_repo_url: str = "",
        focus: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ):
        self._store = store
        self._source_repo = source_repo
        self._source_repo_url = source_repo_url
        self._focus = focus.strip()
        self._progress_cb = progress_cb

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = anthropic.Anthropic(api_key=api_key)

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
        focus_clause = (
            _FOCUS_CLAUSE.format(focus=self._focus) if self._focus else _NO_FOCUS_CLAUSE
        )
        system = _TRIAGE_SYSTEM.format(focus_clause=focus_clause)

        index_lines = []
        for i, sym in enumerate(batch):
            body = (sym.get("body") or sym.get("source") or "").strip()
            signature = body.split("\n")[0][:120]  # first line only
            label = sym.get("label", "fn")
            name = sym.get("name", f"symbol_{i}")
            file_path = sym.get("file_path", "")
            index_lines.append(f"[{i}] {label} `{name}` — {file_path}\n    {signature}")

        index_text = "\n".join(index_lines)
        user_msg = (
            f"Symbol index from '{self._source_repo}' ({len(batch)} symbols):\n\n"
            f"{index_text}\n\n"
            f"Select the indices worth fetching full source code for."
        )

        response = self._client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            temperature=0.1,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            tools=[
                {
                    "name": "select_symbols",
                    "description": "Return the indices of symbols worth examining in full",
                    "input_schema": _TRIAGE_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "select_symbols"},
        )

        result = _parse_tool_result(response, "select_symbols")
        local_indices = result.get("selected", [])
        # Convert batch-local indices to absolute indices into `eligible`
        return [global_offset + i for i in local_indices if 0 <= i < len(batch)]

    # ── Pass 2: describe ────────────────────────────────────────────────────

    def _describe_batch(self, batch: list[dict]) -> int:
        """Send full code for selected symbols; store accepted entries. Returns count added."""
        focus_clause = (
            _FOCUS_CLAUSE.format(focus=self._focus) if self._focus else _NO_FOCUS_CLAUSE
        )
        system = _DESCRIBE_SYSTEM.format(
            repo=self._source_repo, focus_clause=focus_clause
        )

        snippets_text = _format_full_batch(batch)
        user_msg = (
            f"Describe these {len(batch)} pre-selected snippets from '{self._source_repo}'.\n\n"
            f"{snippets_text}"
        )

        response = self._client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            temperature=0.1,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
            tools=[
                {
                    "name": "submit_knowledge",
                    "description": "Submit classified and described knowledge entries",
                    "input_schema": _DESCRIBE_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": "submit_knowledge"},
        )

        result = _parse_tool_result(response, "submit_knowledge")
        entries_data = result.get("entries", [])

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _format_full_batch(batch: list[dict]) -> str:
    parts = []
    for i, symbol in enumerate(batch):
        body = (symbol.get("body") or symbol.get("source") or "").strip()
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS] + "\n... (truncated)"
        label = symbol.get("label", "function")
        name = symbol.get("name", f"symbol_{i}")
        file_path = symbol.get("file_path", "")
        parts.append(f"--- [{i}] {label}: {name} ({file_path}) ---\n{body}")
    return "\n\n".join(parts)


def _parse_tool_result(response, tool_name: str) -> dict:
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            inp = block.input
            if isinstance(inp, str):
                return json.loads(inp)
            return inp if isinstance(inp, dict) else {}
    return {}
