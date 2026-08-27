"""Intelligent summarization of a test run.

A red suite usually fails in fewer ways than it has failing tests: one bad
fixture takes out thirty cases, and the raw runner output buries that under
thirty stack traces. This turns a `TestRunResult` (or a stored raw log) into a
short root-cause summary.

It routes through the `testing` reader stage in `.waterfree/providers.json`, so
by default it runs on a free OpenRouter model and falls back to local Ollama --
see `backend/llm/openrouter_catalog.py`. Summarization is strictly optional: a
failure to produce one never changes the exit code of the test run itself.
"""

from __future__ import annotations

import os

from backend.llm.chat_client import (
    ChatUnavailable,
    chat,
    preflight,
    resolve_chat_target,
)
from backend.testing.results import TestRunResult

STAGE = "testing"
# Only used when no provider claims the `testing` stage, i.e. the local default.
DEFAULT_MODEL = os.environ.get("WATERFREE_TESTING_SUMMARY_MODEL", "freehuntx/qwen3-coder:14b")

_CHAT_TIMEOUT_SECONDS = 180
# Per-failure error text kept before truncation. A traceback's signal is at its
# head and tail; the middle is framework frames.
_ERROR_HEAD_CHARS = 900
_ERROR_TAIL_CHARS = 400
# Failures per map call. Small enough that a free model with a modest context
# window still sees whole tracebacks.
_BATCH_SIZE = 6
_MAP_MAX_TOKENS = int(os.environ.get("WATERFREE_TESTING_SUMMARY_MAP_TOKENS", "400"))
_REDUCE_MAX_TOKENS = int(os.environ.get("WATERFREE_TESTING_SUMMARY_TOKENS", "700"))
# Raw-log summarization has no structure to lean on, so it is chunked by size.
_LOG_CHUNK_CHARS = 8000
_LOG_MAX_CHUNKS = 12

_SYSTEM = (
    "You are a senior engineer triaging a failing test suite. Be concrete and "
    "terse. Never invent file paths, test names, or error text that is not in "
    "the input."
)


def summarize_run(
    result: TestRunResult,
    *,
    workspace_path: str = "",
    fallback_model: str = DEFAULT_MODEL,
) -> str:
    """Root-cause summary for a completed run. Raises `ChatUnavailable` on failure."""
    total = result.passed + result.failed
    if result.failed == 0:
        # No model call: there is nothing to triage, and spending a request to
        # be told "everything passed" is pure latency.
        return f"All {total} test(s) passed."

    target = _target(workspace_path, fallback_model)
    preflight(target)

    failures = [r for r in result.results if not r.passed]
    batches = [failures[i:i + _BATCH_SIZE] for i in range(0, len(failures), _BATCH_SIZE)]
    notes: list[str] = []
    for index, batch in enumerate(batches, start=1):
        body = "\n\n".join(_render_failure(r) for r in batch)
        notes.append(chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": (
                    f"Failing tests (batch {index} of {len(batches)}):\n\n{body}\n\n"
                    "For each distinct root cause in this batch, give one line: "
                    "the cause, then the test names it explains. No preamble."
                )},
            ],
            target=target,
            max_tokens=_MAP_MAX_TOKENS,
            timeout=_CHAT_TIMEOUT_SECONDS,
        ))

    return _reduce(
        notes,
        target=target,
        header=(
            f"Test run: {result.passed} passed, {result.failed} failed "
            f"out of {total}."
        ),
    )


def summarize_log(
    raw_output: str,
    *,
    workspace_path: str = "",
    fallback_model: str = DEFAULT_MODEL,
) -> str:
    """Root-cause summary for stored raw runner output, with no structured results."""
    text = (raw_output or "").strip()
    if not text:
        raise ValueError("No test log to summarize. Run the suite first.")

    target = _target(workspace_path, fallback_model)
    preflight(target)

    chunks = [text[i:i + _LOG_CHUNK_CHARS] for i in range(0, len(text), _LOG_CHUNK_CHARS)]
    if len(chunks) > _LOG_MAX_CHUNKS:
        # Head and tail of a runner log carry the failures and the summary line;
        # the middle is passing-test noise.
        half = _LOG_MAX_CHUNKS // 2
        chunks = chunks[:half] + chunks[-half:]

    notes: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        notes.append(chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": (
                    f"Test runner output (part {index} of {len(chunks)}):\n\n{chunk}\n\n"
                    "List only the failures and errors visible here, one line each, "
                    "with the test name and the reason. Reply with the single word "
                    "NONE if this part shows no failures."
                )},
            ],
            target=target,
            max_tokens=_MAP_MAX_TOKENS,
            timeout=_CHAT_TIMEOUT_SECONDS,
        ))

    useful = [n for n in notes if n.strip() and n.strip().upper() != "NONE"]
    if not useful:
        return "No failures found in the stored test log."
    return _reduce(useful, target=target, header="Test log summary.")


def describe_target(workspace_path: str = "", fallback_model: str = DEFAULT_MODEL) -> str:
    """Which model would serve a summary, for `--help`-style reporting."""
    try:
        return _target(workspace_path, fallback_model).describe()
    except ChatUnavailable as exc:
        return f"unavailable ({exc})"


def _target(workspace_path: str, fallback_model: str):
    return resolve_chat_target(
        stage=STAGE,
        workspace_path=workspace_path,
        fallback_model=fallback_model,
    )


def _reduce(notes: list[str], *, target, header: str) -> str:
    joined = "\n".join(f"- {note.strip()}" for note in notes if note.strip())
    if not joined:
        return header
    return chat(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": (
                f"{header}\n\nPer-batch triage notes:\n{joined}\n\n"
                "Merge these into a single summary: group the failures by root "
                "cause, most impactful first; for each, name the likely culprit "
                "and the single next thing to check. Finish with one line naming "
                "the one fix most likely to turn the suite green."
            )},
        ],
        target=target,
        max_tokens=_REDUCE_MAX_TOKENS,
        timeout=_CHAT_TIMEOUT_SECONDS,
    ).strip()


def _render_failure(result) -> str:
    error = (result.error or "").strip() or "(no error text captured)"
    if len(error) > _ERROR_HEAD_CHARS + _ERROR_TAIL_CHARS:
        error = (
            error[:_ERROR_HEAD_CHARS]
            + "\n... [truncated] ...\n"
            + error[-_ERROR_TAIL_CHARS:]
        )
    return f"### {result.name}\n{error}"
