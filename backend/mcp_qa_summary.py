"""
MCP server — large-file/url QA + summarization via local Ollama.

This intentionally stays simple:
  - One tool (`qa_summary`)
  - Two parameters (`file_or_url`, `question`)
  - One local model (`qwen2.5:14b`)

Run:
    python -m backend.mcp_qa_summary

Register with Claude Code:
    claude mcp add waterfree-qa-summary python -- -m backend.mcp_qa_summary
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser

from backend.mcp_logging import configure_mcp_logger, instrument_tool
from backend.mcp_runtime import FastMCP
from backend.tutorializer import ollama as ollama_client

mcp = FastMCP("waterfree-qa-summary")
log, LOG_FILE = configure_mcp_logger("waterfree-qa-summary")

_DEFAULT_MODEL = os.environ.get("WATERFREE_QA_SUMMARY_MODEL", "qwen2.5:14b")
_DEFAULT_OLLAMA_BASE = os.environ.get("WATERFREE_OLLAMA_BASE", "http://localhost:11434")
_READ_TIMEOUT_SECONDS = 45
_OLLAMA_TIMEOUT_SECONDS = 240
_CHUNK_SIZE_CHARS = 12000
_REDUCTION_BATCH_SIZE = 6


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

    # Keep behavior simple and resilient across mixed encodings.
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
            # Avoid very short chunks when no good break exists.
            if best >= int(max_chars * 0.6):
                end = cursor + best + 1

        chunk = normalized[cursor:end].strip()
        if chunk:
            chunks.append(chunk)

        if end <= cursor:
            end = min(cursor + max_chars, length)
        cursor = end

    return chunks


def _ensure_ollama_ready(model: str) -> None:
    if not ollama_client.ensure_daemon(base=_DEFAULT_OLLAMA_BASE):
        raise RuntimeError(
            "Ollama is not reachable at "
            f"{_DEFAULT_OLLAMA_BASE}. Start it with `ollama serve`."
        )

    # Provide an early, clear error if the requested model is not installed.
    models = [name.lower() for name in ollama_client.list_models(base=_DEFAULT_OLLAMA_BASE)]
    model_lower = model.lower()
    if ":" in model_lower:
        installed = model_lower in models or f"{model_lower}:latest" in models
    else:
        installed = any(name.split(":", 1)[0] == model_lower for name in models)
    if not installed:
        available = ", ".join(models[:12]) or "<none>"
        raise RuntimeError(
            f"Ollama model '{model}' is not available. "
            f"Installed models: {available}"
        )


def _ollama_chat(messages: list[dict[str, str]]) -> str:
    return ollama_client.chat(
        model=_DEFAULT_MODEL,
        messages=messages,
        base=_DEFAULT_OLLAMA_BASE,
        timeout=_OLLAMA_TIMEOUT_SECONDS,
    ).strip()


def _analyze_chunk(chunk: str, *, chunk_index: int, chunk_total: int, question: str) -> str:
    system_prompt = (
        "You are a careful technical analyst. Analyze only the provided chunk. "
        "Extract all details relevant to the question. "
        "Be specific, include concrete facts, and do not omit nuances."
    )
    user_prompt = (
        f"Question:\n{question}\n\n"
        f"Chunk {chunk_index} of {chunk_total}:\n"
        "----- BEGIN CHUNK -----\n"
        f"{chunk}\n"
        "----- END CHUNK -----\n\n"
        "Return detailed notes focused on this question."
    )
    return _ollama_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )


def _merge_note_batch(
    batch: list[str],
    *,
    question: str,
    round_index: int,
    batch_index: int,
) -> str:
    system_prompt = (
        "You are combining multiple partial analyses into one coherent, detailed synthesis. "
        "Preserve important details and edge cases. Remove duplicates."
    )
    joined = "\n\n".join(
        f"[analysis {i + 1}]\n{text}" for i, text in enumerate(batch)
    )
    user_prompt = (
        f"Question:\n{question}\n\n"
        f"Reduction round {round_index}, batch {batch_index}.\n"
        "Merge these analyses into one detailed synthesis:\n\n"
        f"{joined}"
    )
    return _ollama_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )


def _reduce_chunk_notes(notes: list[str], question: str) -> str:
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
                    question=question,
                    round_index=round_index,
                    batch_index=batch_index,
                )
            )
        current = merged_round
        round_index += 1
    return current[0]


def _render_final_answer(synthesis: str, *, question: str, file_or_url: str) -> str:
    system_prompt = (
        "You are an expert assistant. Produce a detailed final response that directly "
        "answers the question using the synthesized notes."
    )
    user_prompt = (
        f"Source: {file_or_url}\n\n"
        f"Question:\n{question}\n\n"
        "Synthesized notes:\n"
        f"{synthesis}\n\n"
        "Write a detailed final answer with these sections:\n"
        "1) Direct Answer\n"
        "2) Supporting Details\n"
        "3) Caveats and Unknowns\n"
        "4) Suggested Next Checks"
    )
    return _ollama_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )


def _qa_summary_impl(file_or_url: str, question: str) -> str:
    """Summarize a file/URL and answer a question using local Ollama.

    Args:
        file_or_url: Local file path or HTTP(S) URL.
        question: The question to answer about that content.

    Returns JSON with source metadata and a detailed answer.
    """
    if not file_or_url.strip():
        raise ValueError("file_or_url is required.")
    if not question.strip():
        raise ValueError("question is required.")

    _ensure_ollama_ready(_DEFAULT_MODEL)
    source_text = _read_source_text(file_or_url)
    if not source_text.strip():
        raise RuntimeError("Source content is empty.")

    chunks = _split_into_chunks(source_text, max_chars=_CHUNK_SIZE_CHARS)
    if not chunks:
        raise RuntimeError("No readable text content found.")

    chunk_notes: list[str] = []
    total_chunks = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        chunk_notes.append(
            _analyze_chunk(
                chunk,
                chunk_index=idx,
                chunk_total=total_chunks,
                question=question,
            )
        )

    merged_notes = _reduce_chunk_notes(chunk_notes, question)
    final_answer = _render_final_answer(
        merged_notes,
        question=question,
        file_or_url=file_or_url,
    )

    return json.dumps(
        {
            "source": file_or_url,
            "question": question,
            "model": _DEFAULT_MODEL,
            "source_characters": len(source_text),
            "chunks_processed": total_chunks,
            "response": final_answer,
        },
        indent=2,
    )


qa_summary = mcp.tool()(instrument_tool(log, "qa_summary", _qa_summary_impl))


if __name__ == "__main__":
    log.info("Starting MCP server waterfree-qa-summary (logFile=%s)", LOG_FILE)
    mcp.run()
