---
name: waterfree-qa-summary
description: Map/reduce reader over a large file or URL. Reach for it whenever you would otherwise have to load a huge file or web page into your own context just to extract a few facts.
---

# WaterFree — QA Summary

Local-Ollama-powered map/reduce summarizer. Reads a file or URL, splits into
chunks, fans out an analysis per chunk, then synthesizes a focused answer.

Run via the `waterfree` CLI in whatever shell you have (Bash or PowerShell) —
`waterfree` is on PATH, so the command text is identical in both (every example
below is a single line). Returns JSON on stdout.

## When to Use

- For reading files larger than ~400 lines, or when you'd otherwise read 3+ files
- A file or URL is **too large to read directly** without burning context (long
  log files, vendored source, generated reports, lengthy docs, transcripts).
- You only need **specific information** out of a large document — let the tool
  do the reading and return the relevant facts.
- You want a **structured answer** (Direct Answer / Supporting Details /
  Caveats / Suggested Next Checks) rather than raw text.
- You are scanning a **third-party doc page or RFC** for one specific question.

## When NOT to Use

- You need to edit, grep, or quote the file precisely — use Read / Grep instead.
- The file is small enough to read directly (a few hundred lines).
- You need real-time or authenticated web content — the fetcher is plain HTTP,
  no JS rendering, no auth.

## CLI

```bash
waterfree qa-summary ask <file-or-url> -q "<your question>"
```

Examples:
```bash
waterfree qa-summary ask ./logs/build_2026_05_22.txt -q "What was the first error that caused the build to fail?"

waterfree qa-summary ask https://www.rfc-editor.org/rfc/rfc9110 -q "What does the spec say about idempotent PATCH requests?"
```

Output shape:
```json
{
  "source": "<file path or URL>",
  "question": "<the question>",
  "model": "qwen2.5:14b",
  "source_characters": 124857,
  "chunks_processed": 11,
  "response": "1) Direct Answer ... 2) Supporting Details ... 3) Caveats ... 4) Suggested Next Checks"
}
```

| Argument | Notes |
|----------|-------|
| `<file-or-url>` | Absolute path, relative path (resolved against CWD), or `http(s)://` URL. HTML pages have script/style stripped automatically. |
| `-q / --question` | Be specific. The tool focuses every chunk-level analysis on this question, so vague prompts yield vague answers. |

## Requirements

- Local Ollama daemon running at the default base URL (`http://localhost:11434`),
  or set `WATERFREE_OLLAMA_BASE`.
- The `qwen2.5:14b` model installed by default, or set `WATERFREE_QA_SUMMARY_MODEL`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 2    | Missing question or invalid source |
| 4    | Ollama not reachable or model not installed |
| 1    | Internal error |
