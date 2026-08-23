"""
Large-file / URL QA + summarization.

Reads a file or URL, splits into chunks, fans out one analysis per chunk,
reduces them into a single synthesis, then renders a final answer.

The model is chosen by `backend.llm.chat_client` from the `qa_summary` stage in
`.waterfree/providers.json`, so this runs against either the local Ollama GPU
(the default) or a remote gateway such as OpenRouter. Every request in one run
uses the same target, resolved once up front.

Previously lived in `backend/mcp_qa_summary.py`; extracted so the logic
remains after the MCP server scaffolding is removed.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
from typing import Any

from backend.llm.chat_client import ChatTarget, ChatUnavailable, chat, preflight, resolve_chat_target

_STAGE = "qa_summary"
# Used only when no provider claims the qa_summary stage, i.e. the local default.
_DEFAULT_MODEL = os.environ.get("WATERFREE_QA_SUMMARY_MODEL", "freehuntx/qwen3-coder:14b")
_READ_TIMEOUT_SECONDS = 45
_CHAT_TIMEOUT_SECONDS = 240
_CHUNK_SIZE_CHARS = 12000
_REDUCTION_BATCH_SIZE = 6
# Concurrent chunk analyses against a hosted gateway. Deliberately small: the
# ceiling here is the provider's per-minute limit, not local CPU, and a run that
# trips rate limiting is slower than the serial version it replaced.
_REMOTE_ANALYSIS_WORKERS = int(os.environ.get("WATERFREE_QA_SUMMARY_WORKERS", "4"))
_ANALYSIS_MAX_TOKENS = int(os.environ.get("WATERFREE_QA_SUMMARY_ANALYSIS_TOKENS", "512"))
_FINAL_MAX_TOKENS = int(os.environ.get("WATERFREE_QA_SUMMARY_FINAL_TOKENS", "256"))
_DETAILED_FINAL_MAX_TOKENS = int(os.environ.get("WATERFREE_QA_SUMMARY_DETAILED_TOKENS", "1024"))


class OllamaUnavailable(ChatUnavailable):
    """Backwards-compatible alias for :class:`ChatUnavailable`.

    Kept so existing callers that catch `OllamaUnavailable` keep working now
    that the provider is not necessarily Ollama. Catch `ChatUnavailable` in new
    code -- it is the type actually raised.
    """


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._suppress_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._suppress_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._suppress_depth > 0:
            self._suppress_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._suppress_depth == 0 and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        merged = "\n".join(self._chunks)
        merged = unescape(merged)
        merged = re.sub(r"\n{3,}", "\n\n", merged)
        return merged.strip()


def _is_url(path_or_url: str) -> bool:
    parsed = urllib.parse.urlparse(path_or_url)
    return parsed.scheme in {"http", "https"}


def _read_source_text(file_or_url: str) -> str:
    if _is_url(file_or_url):
        return _read_url_text(file_or_url)
    return _read_file_text(file_or_url)


def _read_file_text(path: str) -> str:
    resolved = os.path.abspath(path)
    if not os.path.exists(resolved):
        raise FileNotFoundError(f"File not found: {resolved}")
    if not os.path.isfile(resolved):
        raise ValueError(f"Path is not a file: {resolved}")
    with open(resolved, "rb") as handle:
        raw = handle.read()
    return raw.decode("utf-8", errors="replace")


def _read_url_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "waterfree-qa-summary/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_READ_TIMEOUT_SECONDS) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "").lower()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} while fetching URL: {url}\n{body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not fetch URL: {url}\n{exc}") from exc

    text = raw.decode(charset, errors="replace")
    if "html" in content_type or "<html" in text[:1000].lower():
        parser = _HTMLTextExtractor()
        parser.feed(text)
        parsed = parser.text()
        if parsed:
            return parsed
    return text


def _split_into_chunks(text: str, max_chars: int = _CHUNK_SIZE_CHARS) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    chunks: list[str] = []
    cursor = 0
    length = len(normalized)

    while cursor < length:
        end = min(cursor + max_chars, length)
        if end < length:
            window = normalized[cursor:end]
            break_candidates = [
                window.rfind("\n\n"),
                window.rfind("\n"),
                window.rfind(". "),
                window.rfind(" "),
            ]
            best = max(break_candidates)
            if best >= int(max_chars * 0.6):
                end = cursor + best + 1

        chunk = normalized[cursor:end].strip()
        if chunk:
            chunks.append(chunk)

        if end <= cursor:
            end = min(cursor + max_chars, length)
        cursor = end

    return chunks


def _analyze_chunk(
    chunk: str,
    *,
    target: ChatTarget,
    chunk_index: int,
    chunk_total: int,
    question: str,
) -> str:
    system_prompt = (
        "You are a careful technical analyst. Analyze only the provided chunk. "
        "Extract only facts relevant to the question. Be concise."
    )
    user_prompt = (
        f"Question:\n{question}\n\n"
        f"Chunk {chunk_index} of {chunk_total}:\n"
        "----- BEGIN CHUNK -----\n"
        f"{chunk}\n"
        "----- END CHUNK -----\n\n"
        "Return concise notes focused on this question. Do not answer unrelated points."
    )
    return _chat(target, system_prompt, user_prompt, max_tokens=_ANALYSIS_MAX_TOKENS)


def _analyze_chunks(
    chunks: list[str],
    *,
    target: ChatTarget,
    question: str,
) -> list[str]:
    """
    Map the analysis prompt over every chunk, returning notes in source order.

    Serial against a local model: Ollama serves one request at a time per loaded
    model, so concurrency there buys nothing and only competes for the same GPU.
    Against a hosted gateway the requests are independent and the wall-clock is
    dominated by round-trips, so a small pool is most of the win.

    The pool is capped rather than sized to the chunk count: a large document
    would otherwise open dozens of simultaneous connections and trip per-minute
    gateway limits, turning a latency win into a run of 429s.
    """
    total = len(chunks)

    def analyze(index: int, chunk: str) -> str:
        return _analyze_chunk(
            chunk,
            target=target,
            chunk_index=index,
            chunk_total=total,
            question=question,
        )

    if target.is_local or total < 2:
        return [analyze(idx, chunk) for idx, chunk in enumerate(chunks, start=1)]

    workers = min(_REMOTE_ANALYSIS_WORKERS, total)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qa-chunk") as pool:
        # map() preserves input order, which the reduction tree depends on —
        # notes must stay in source order or the merge reads out of sequence.
        return list(pool.map(lambda pair: analyze(*pair), enumerate(chunks, start=1)))


def _merge_note_batch(
    batch: list[str],
    *,
    target: ChatTarget,
    question: str,
    round_index: int,
    batch_index: int,
) -> str:
    system_prompt = (
        "Combine multiple partial analyses into one concise synthesis. "
        "Preserve only details needed to answer the question. Remove duplicates."
    )
    joined = "\n\n".join(f"[analysis {i + 1}]\n{text}" for i, text in enumerate(batch))
    user_prompt = (
        f"Question:\n{question}\n\n"
        f"Reduction round {round_index}, batch {batch_index}.\n"
        "Merge these analyses into concise answer notes:\n\n"
        f"{joined}"
    )
    return _chat(target, system_prompt, user_prompt, max_tokens=_ANALYSIS_MAX_TOKENS)


def _reduce_chunk_notes(notes: list[str], question: str, *, target: ChatTarget) -> str:
    if not notes:
        return ""
    if len(notes) == 1:
        return notes[0]

    current = notes[:]
    round_index = 1
    while len(current) > 1:
        merged_round: list[str] = []
        for offset in range(0, len(current), _REDUCTION_BATCH_SIZE):
            batch_index = (offset // _REDUCTION_BATCH_SIZE) + 1
            batch = current[offset:offset + _REDUCTION_BATCH_SIZE]
            if len(batch) == 1:
                merged_round.append(batch[0])
                continue
            merged_round.append(
                _merge_note_batch(
                    batch,
                    target=target,
                    question=question,
                    round_index=round_index,
                    batch_index=batch_index,
                )
            )
        current = merged_round
        round_index += 1
    return current[0]


def _render_final_answer(
    synthesis: str,
    *,
    target: ChatTarget,
    question: str,
    file_or_url: str,
) -> str:
    system_prompt = (
        "You answer questions using synthesized notes. Answer directly and stop. "
        "Do not add sections, caveats, suggested checks, or extra explanation unless the question asks for them."
    )
    user_prompt = (
        f"Source: {file_or_url}\n\n"
        f"Question:\n{question}\n\n"
        "Synthesized notes:\n"
        f"{synthesis}\n\n"
        "Answer the question directly. Use the shortest complete answer that satisfies the question."
    )
    return _chat(target, system_prompt, user_prompt, max_tokens=_final_token_budget(question))


def _chat(target: ChatTarget, system_prompt: str, user_prompt: str, *, max_tokens: int) -> str:
    return chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        target=target,
        max_tokens=max_tokens,
        timeout=_CHAT_TIMEOUT_SECONDS,
    )


def _final_token_budget(question: str) -> int:
    q = question.lower()
    detail_markers = (
        "detailed",
        "detail",
        "explain",
        "walk me through",
        "step by step",
        "thorough",
        "comprehensive",
        "full answer",
        "deep dive",
    )
    if any(marker in q for marker in detail_markers):
        return _DETAILED_FINAL_MAX_TOKENS
    return _FINAL_MAX_TOKENS


def run_qa_summary(
    file_or_url: str,
    question: str,
    *,
    workspace_path: str = "",
    document: Any | None = None,
) -> dict:
    """Read a file or URL, chunk it, summarize it, answer the question.

    The provider comes from the `qa_summary` stage in the workspace's
    `.waterfree/providers.json`; with nothing routed there it falls back to
    local Ollama. `document` is an already-loaded profile (the extension host
    passes one because it carries API keys from SecretStorage); the CLI leaves
    it None and the profile is read from disk.

    Returns a dict suitable for JSON serialization. Raises ChatUnavailable when
    the target cannot serve the request.
    """
    if not file_or_url.strip():
        raise ValueError("file_or_url is required.")
    if not question.strip():
        raise ValueError("question is required.")

    target = resolve_chat_target(
        stage=_STAGE,
        workspace_path=workspace_path,
        document=document,
        fallback_model=_DEFAULT_MODEL,
    )
    # One preflight per run rather than a failure on chunk 1 of N.
    preflight(target)

    source_text = _read_source_text(file_or_url)
    if not source_text.strip():
        raise RuntimeError("Source content is empty.")

    chunks = _split_into_chunks(source_text, max_chars=_CHUNK_SIZE_CHARS)
    if not chunks:
        raise RuntimeError("No readable text content found.")

    total_chunks = len(chunks)
    chunk_notes = _analyze_chunks(chunks, target=target, question=question)

    merged_notes = _reduce_chunk_notes(chunk_notes, question, target=target)
    final_answer = _render_final_answer(
        merged_notes,
        target=target,
        question=question,
        file_or_url=file_or_url,
    )

    return {
        "source": file_or_url,
        "question": question,
        "model": target.model,
        "provider": target.provider_type,
        "source_characters": len(source_text),
        "chunks_processed": total_chunks,
        "response": final_answer,
    }
