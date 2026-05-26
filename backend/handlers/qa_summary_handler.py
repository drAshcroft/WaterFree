from __future__ import annotations

import os
import urllib.parse
import json
from typing import Any

from backend.qa_summary.core import run_qa_summary

_qa_summary_impl = run_qa_summary


def handle_run_qa_summary(server: Any, params: dict) -> dict:
    workspace_path = os.path.abspath(str(params.get("workspacePath") or "."))
    file_or_url = str(
        params.get("fileOrUrl")
        or params.get("file_or_url")
        or ""
    ).strip()
    question = str(params.get("question") or "").strip()

    if not file_or_url:
        raise ValueError("fileOrUrl is required.")
    if not question:
        raise ValueError("question is required.")

    source = _resolve_source(workspace_path, file_or_url)
    result = _qa_summary_impl(source, question)
    if isinstance(result, str):
        return json.loads(result)
    return result


def _resolve_source(workspace_path: str, file_or_url: str) -> str:
    parsed = urllib.parse.urlparse(file_or_url)
    if parsed.scheme in {"http", "https"} or os.path.isabs(file_or_url):
        return file_or_url
    return os.path.abspath(os.path.join(workspace_path, file_or_url))
