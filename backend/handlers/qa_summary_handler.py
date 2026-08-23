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
    # The synced profile carries API keys exported from VS Code SecretStorage;
    # the copy on disk never does, so hand the in-memory one to the resolver.
    result = _qa_summary_impl(
        source,
        question,
        workspace_path=workspace_path,
        document=_synced_profile(server, workspace_path),
    )
    if isinstance(result, str):
        return json.loads(result)
    return result


def _synced_profile(server: Any, workspace_path: str) -> Any | None:
    getter = getattr(server, "_get_provider_profile", None)
    if not callable(getter):
        return None
    try:
        return getter(workspace_path)
    except Exception:
        # A missing or malformed profile must not break qa-summary; the resolver
        # falls back to local Ollama when handed None.
        return None


def _resolve_source(workspace_path: str, file_or_url: str) -> str:
    parsed = urllib.parse.urlparse(file_or_url)
    if parsed.scheme in {"http", "https"} or os.path.isabs(file_or_url):
        return file_or_url
    return os.path.abspath(os.path.join(workspace_path, file_or_url))
