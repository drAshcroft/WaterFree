"""
Minimal Ollama client — no external dependencies beyond stdlib.

Two modes:
  REST  — talks to a running Ollama daemon at http://localhost:11434.
  CLI   — shells out to the `ollama` binary (list, serve) when the daemon
          is not yet running.  Useful when Ollama is installed as a native
          command-line tool rather than inside a Docker container.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_DEFAULT_BASE = "http://localhost:11434"

# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def cli_path() -> str | None:
    """Return the absolute path to the `ollama` binary, or None if not found."""
    return shutil.which("ollama")


def is_cli_available() -> bool:
    """Return True when the `ollama` binary is on PATH."""
    return cli_path() is not None


def list_models_cli() -> list[str]:
    """
    Return model names by running ``ollama list``.

    Raises:
        OllamaError  if the CLI is not found or the command fails.
    """
    binary = cli_path()
    if not binary:
        raise OllamaError(
            "ollama CLI not found on PATH.\n"
            "Install Ollama from https://ollama.com and ensure it is in your PATH."
        )
    try:
        result = subprocess.run(
            [binary, "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise OllamaError(f"Could not execute '{binary}': {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise OllamaError("'ollama list' timed out after 15 s") from exc

    if result.returncode != 0:
        raise OllamaError(f"'ollama list' failed (exit {result.returncode}): {result.stderr.strip()}")

    models: list[str] = []
    for line in result.stdout.splitlines():
        # Header row: "NAME   ID   SIZE   MODIFIED"
        if line.lower().startswith("name"):
            continue
        parts = line.split()
        if parts:
            # Strip the ":latest" tag that Ollama appends by default
            name = parts[0]
            if ":" in name:
                name = name.split(":")[0]
            if name:
                models.append(name)
    return models


def ensure_daemon(base: str = _DEFAULT_BASE, ping_timeout: int = 3) -> bool:
    """
    Make sure the Ollama daemon is reachable.  If it is not, attempt to start
    it via ``ollama serve`` (detached background process).

    Returns:
        True   — daemon was already running (or was successfully started and
                 responded within ~5 s).
        False  — CLI not available or daemon did not respond after start attempt.

    Does NOT raise — callers should check the return value and fall back
    gracefully.
    """
    # Fast path: already running
    if _ping(base, ping_timeout):
        return True

    binary = cli_path()
    if not binary:
        log.warning("Ollama CLI not found; cannot start daemon automatically.")
        return False

    log.info("Ollama daemon not running — starting 'ollama serve' in background.")
    try:
        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            # Detach from the current console on Windows
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        subprocess.Popen([binary, "serve"], **kwargs)
    except Exception as exc:
        log.warning("Failed to start 'ollama serve': %s", exc)
        return False

    # Give the daemon up to ~5 s to come up
    import time
    for _ in range(10):
        time.sleep(0.5)
        if _ping(base, 2):
            log.info("Ollama daemon is now reachable at %s.", base)
            return True

    log.warning("Started 'ollama serve' but daemon not reachable at %s yet.", base)
    return False


def _ping(base: str, timeout: int) -> bool:
    """Return True if the Ollama REST API responds."""
    try:
        urllib.request.urlopen(f"{base}/api/tags", timeout=timeout)
        return True
    except Exception:
        return False


class OllamaError(RuntimeError):
    """Raised when the Ollama daemon is unreachable or returns an error."""


def list_models(base: str = _DEFAULT_BASE) -> list[str]:
    """Return the names of all models available in the local Ollama installation."""
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])]
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"Cannot connect to Ollama at {base}: {exc}\n"
            "Make sure Ollama is running: ollama serve"
        ) from exc
    except Exception as exc:
        raise OllamaError(f"Unexpected error listing Ollama models: {exc}") from exc


def chat(
    model: str,
    messages: list[dict],
    base: str = _DEFAULT_BASE,
    timeout: int = 180,
) -> str:
    """
    Send a chat request to Ollama and return the assistant's response text.

    messages: list of {"role": "system"|"user"|"assistant", "content": "..."}
    """
    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{base}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data["message"]["content"]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise OllamaError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"Cannot reach Ollama at {base}: {exc}\n"
            "Make sure Ollama is running: ollama serve"
        ) from exc
    except Exception as exc:
        raise OllamaError(f"Ollama request failed: {exc}") from exc
