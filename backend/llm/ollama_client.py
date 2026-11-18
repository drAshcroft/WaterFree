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
import os
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


class OllamaTimeout(OllamaError):
    """The daemon accepted the request but did not answer in time.

    Distinct from "unreachable": a refused connection means Ollama is not
    running, while a timeout almost always means it is running but starved --
    other models holding the GPU, or CPU-heavy processes sharing the host.
    Callers that can wait longer or retry need to tell the two apart.
    """

    def __init__(self, message: str, *, timeout: float, endpoint: str) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.endpoint = endpoint


class OllamaHTTPError(OllamaError):
    """The daemon answered with an HTTP error status (404 unknown model, 503 busy, ...)."""

    def __init__(self, message: str, *, status: int, detail: str) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


# /api/tags only reads manifests, so 5 s is generous on an idle host. Callers
# that run on a busy machine (see backend/vision) pass a longer `timeout`.
LIST_TIMEOUT_SECONDS = 5.0


def _is_timeout(exc: BaseException) -> bool:
    """True for a socket timeout, whether raw or wrapped by urllib.

    urllib wraps a timeout during connect/send in URLError, but a timeout while
    waiting for the response headers or body escapes as a bare TimeoutError.
    """
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


def _timeout_error(base: str, endpoint: str, timeout: float) -> OllamaTimeout:
    return OllamaTimeout(
        f"Ollama at {base} did not answer {endpoint} in {timeout:g} s; "
        "the host may be under load (other models or GPU/CPU-heavy processes).",
        timeout=timeout,
        endpoint=endpoint,
    )


def list_models(base: str = _DEFAULT_BASE, timeout: float = LIST_TIMEOUT_SECONDS) -> list[str]:
    """Return the names of all models available in the local Ollama installation.

    Raises OllamaTimeout when the daemon is up but slow, OllamaError otherwise.
    """
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read())
        return [m["name"] for m in data.get("models", [])]
    except Exception as exc:
        if _is_timeout(exc):
            raise _timeout_error(base, "/api/tags", timeout) from exc
        if isinstance(exc, urllib.error.URLError):
            raise OllamaError(
                f"Cannot connect to Ollama at {base}: {exc}\n"
                "Make sure Ollama is running: ollama serve"
            ) from exc
        raise OllamaError(f"Unexpected error listing Ollama models: {exc}") from exc


def chat(
    model: str,
    messages: list[dict],
    base: str = _DEFAULT_BASE,
    timeout: float = 180,
    keep_alive: str | None = None,
    options: dict | None = None,
    images: list[str] | None = None,
    response_format: str | dict | None = None,
) -> str:
    """
    Send a chat request to Ollama and return the assistant's response text.

    messages: list of {"role": "system"|"user"|"assistant", "content": "..."}
    images:   base64-encoded image payloads (no data: prefix) for a vision
              model. Attached to the LAST user message, which is where Ollama
              expects them -- a multimodal turn is one message carrying both
              the question and its pixels, not a separate image message.
    response_format: Ollama's `format` field -- "json", or a JSON schema dict
              that constrains decoding to that shape (structured outputs).

    Raises OllamaTimeout when no answer arrives within `timeout` seconds,
    OllamaHTTPError when the daemon answers with an error status (404 for a
    model that is not downloaded), and OllamaError for anything else.
    """
    payload = {
        "model": model,
        "messages": _attach_images(messages, images),
        "stream": False,
    }
    if os.environ.get("WATERFREE_OLLAMA_THINK", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        payload["think"] = False
    if keep_alive:
        payload["keep_alive"] = keep_alive
    if options:
        payload["options"] = options
    if response_format:
        payload["format"] = response_format
    body = json.dumps(payload).encode()

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
        raise OllamaHTTPError(
            f"Ollama HTTP {exc.code}: {detail}", status=exc.code, detail=detail,
        ) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        if _is_timeout(exc):
            raise _timeout_error(base, "/api/chat", timeout) from exc
        raise OllamaError(
            f"Cannot reach Ollama at {base}: {exc}\n"
            "Make sure Ollama is running: ollama serve"
        ) from exc
    except Exception as exc:
        raise OllamaError(f"Ollama request failed: {exc}") from exc


def _attach_images(messages: list[dict], images: list[str] | None) -> list[dict]:
    """Return `messages` with `images` bolted onto the last user turn."""
    if not images:
        return messages
    copied = [dict(m) for m in messages]
    for message in reversed(copied):
        if message.get("role") == "user":
            message["images"] = list(images)
            return copied
    # No user turn to attach to: synthesise one rather than silently dropping
    # the pixels, which would produce a confident answer about nothing.
    copied.append({"role": "user", "content": "", "images": list(images)})
    return copied


def pull(
    model: str,
    base: str = _DEFAULT_BASE,
    timeout: int = 3600,
    on_progress: "callable | None" = None,
) -> None:
    """Download `model` into the local Ollama store, blocking until complete.

    Streams NDJSON status lines; `on_progress` receives each status string.
    A generous default timeout: these are multi-gigabyte downloads.
    """
    req = urllib.request.Request(
        f"{base}/api/pull",
        data=json.dumps({"model": model, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("error"):
                    raise OllamaError(f"Pull of '{model}' failed: {event['error']}")
                if on_progress is not None:
                    on_progress(str(event.get("status", "")))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise OllamaError(f"Ollama HTTP {exc.code} pulling '{model}': {detail}") from exc
    except urllib.error.URLError as exc:
        raise OllamaError(f"Cannot reach Ollama at {base}: {exc}") from exc
    except OllamaError:
        raise
    except Exception as exc:
        raise OllamaError(f"Pull of '{model}' failed: {exc}") from exc


def has_model(
    model: str,
    base: str = _DEFAULT_BASE,
    *,
    strict: bool = False,
    timeout: float = LIST_TIMEOUT_SECONDS,
) -> bool:
    """True when `model` is already downloaded.

    With `strict`, a failure to list models raises (OllamaTimeout when the
    daemon is merely slow) instead of reporting the model as missing.
    """
    try:
        installed = list_models(base=base, timeout=timeout)
    except OllamaError:
        if strict:
            raise
        return False
    return model_in(model, installed)


def model_in(model: str, installed: list[str]) -> bool:
    """True when `model` is among `installed` model names.

    Tag-aware: a bare name matches any tag, while an explicit tag must match
    exactly (with ":latest" implied), mirroring how Ollama itself resolves ids.
    """
    installed = [name.lower() for name in installed]
    wanted = model.strip().lower()
    if ":" in wanted:
        return wanted in installed or f"{wanted}:latest" in installed
    return any(name.split(":", 1)[0] == wanted for name in installed)
