"""Ollama's side of GPU scheduling: who is resident, eviction, and the shared lease.

Ollama keeps a model on the GPU for its ``keep_alive`` window after each call
(15-30 min for WaterFree's areas), so a vision call made an hour ago can still
be holding 6 GB when a mesh job starts. Heavy jobs therefore evict every
resident model right after taking the exclusive lease.

The shared lease applies only to the *local* Ollama daemon that shares this
machine's GPU -- loopback host on the daemon's own port. A remote base URL, or
a test double on some ephemeral port, is not a GPU user here.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager, nullcontext
from urllib.parse import urlparse

from backend.gpu import lease

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
_DEFAULT_PORT = 11434

SCOPE_PROCESS = "process"   # one-shot CLI: hold shared from the first call until exit
SCOPE_CALL = "call"         # long-lived server (`waterfree serve`): per request
_scope = SCOPE_PROCESS


def set_scope(scope: str) -> None:
    global _scope
    if scope not in (SCOPE_PROCESS, SCOPE_CALL):
        raise ValueError(f"unknown Ollama lease scope: {scope}")
    _scope = scope


def local_base() -> str:
    """The local daemon's base URL, honouring WATERFREE_OLLAMA_BASE and OLLAMA_HOST."""
    explicit = os.environ.get("WATERFREE_OLLAMA_BASE", "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if host:
        if "://" not in host:
            host = f"http://{host}"
        parsed = urlparse(host)
        port = parsed.port or _DEFAULT_PORT
        hostname = parsed.hostname or "localhost"
        if hostname in ("0.0.0.0", "::"):
            hostname = "localhost"
        return f"{parsed.scheme or 'http'}://{hostname}:{port}"
    return f"http://localhost:{_DEFAULT_PORT}"


def _daemon_port() -> int:
    return urlparse(local_base()).port or _DEFAULT_PORT


def uses_local_gpu(base: str) -> bool:
    """True when ``base`` is the daemon on this machine, i.e. it competes for this GPU."""
    parsed = urlparse(base if "://" in base else f"http://{base}")
    host = (parsed.hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        return False
    port = parsed.port or (_DEFAULT_PORT if parsed.scheme in ("http", "") else None)
    return port == _daemon_port()


def access(base: str, *, label: str = "") -> AbstractContextManager:
    """Context for one Ollama request: waits while an exclusive GPU job runs.

    In process scope the shared lease is taken on first use and kept until the
    command exits; in call scope it is held for this request only.
    """
    if not uses_local_gpu(base) or not lease.scheduler_enabled():
        return nullcontext()
    if _scope == SCOPE_PROCESS:
        lease.ensure_process_shared(label=label or lease._default_label(), detail="ollama")
        return nullcontext()
    return lease.hold(lease.SHARED, label=label or "serve", detail="ollama")


# ---------------------------------------------------------------------------
# Residency and eviction
# ---------------------------------------------------------------------------

def resident_models(base: str | None = None, timeout: float = 3.0) -> list[dict] | None:
    """Models currently loaded by the daemon, or None when it is unreachable."""
    base = (base or local_base()).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (OSError, ValueError, urllib.error.URLError):
        return None
    out = []
    for model in data.get("models", []) or []:
        out.append({
            "name": model.get("name") or model.get("model", ""),
            "vramGb": round((model.get("size_vram") or 0) / 1024 ** 3, 2),
            "expiresAt": model.get("expires_at"),
        })
    return out


def evict_all(base: str | None = None, *, settle_seconds: float = 15.0) -> list[str]:
    """Unload every resident model; return the names that were unloaded.

    Uses the same request as ``ollama stop``: a generate call with an empty
    prompt and ``keep_alive: 0``. Embedding-only models reject generate, so they
    get the equivalent embed call. Waits until ``/api/ps`` is empty (or the
    settle time runs out) because unloading is asynchronous.
    """
    base = (base or local_base()).rstrip("/")
    resident = resident_models(base)
    if not resident:
        return []
    names = [m["name"] for m in resident if m["name"]]
    for name in names:
        if not _post(f"{base}/api/generate", {"model": name, "keep_alive": 0}):
            _post(f"{base}/api/embed", {"model": name, "input": "", "keep_alive": 0})
    deadline = time.monotonic() + settle_seconds
    while time.monotonic() < deadline:
        remaining = resident_models(base)
        if not remaining:
            break
        time.sleep(0.5)
    return names


def _post(url: str, payload: dict, timeout: float = 30.0) -> bool:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            resp.read()
        return True
    except (OSError, urllib.error.URLError):
        return False
