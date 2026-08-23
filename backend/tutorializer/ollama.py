"""Deprecated shim — the Ollama client now lives in `backend.llm.ollama_client`.

It moved when qa-summary and tutorial generation gained the ability to run
against a remote gateway (OpenRouter) instead of the local GPU: a client shared
by `backend.qa_summary` and `backend.tutorializer` does not belong under either
one. Prefer `backend.llm.chat_client.chat()` for new call sites — it picks the
provider from `.waterfree/providers.json` instead of hardcoding local Ollama.
"""
from __future__ import annotations

from backend.llm.ollama_client import (  # noqa: F401
    OllamaError,
    chat,
    cli_path,
    ensure_daemon,
    is_cli_available,
    list_models,
    list_models_cli,
)

__all__ = [
    "OllamaError",
    "chat",
    "cli_path",
    "ensure_daemon",
    "is_cli_available",
    "list_models",
    "list_models_cli",
]
