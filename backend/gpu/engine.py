"""Run a native inference server (trellis-server, moss-tts-server) for one command.

The server is a child of the WaterFree command that needs it and lives exactly
as long as that command holds the GPU lease:

* It is bound to 127.0.0.1 on a free ephemeral port, never a fixed one, so two
  WaterFree checkouts or a user's own server on :8080 cannot collide with it.
* On Windows it is placed in a Job Object with KILL_ON_JOB_CLOSE. If the
  command dies -- Ctrl+C, a closed terminal, an agent's tool timeout killing the
  process tree -- the OS closes the job handle and kills the server with it, so
  a crashed command can never leave 10 GB of weights pinned on the card.
* Its stdout/stderr go to a log file under ``logs/engines``; failures quote the
  tail of that log, because the server's own message is almost always the
  useful part ("failed to allocate", "unknown tensor", ...).
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Self

from backend.app_paths import engine_logs_dir
from backend.gpu.spill import SpillWatch

_KEEP_LOGS = 40
_live: set[EngineServer] = set()
_live_lock = threading.Lock()


class EngineError(RuntimeError):
    """The engine failed to start, crashed, or rejected a request."""

    def __init__(self, message: str, *, log_path: Path | None = None, log_tail: str = "",
                 status: int | None = None) -> None:
        super().__init__(message)
        self.log_path = log_path
        self.log_tail = log_tail
        self.status = status


class EngineServer:
    """A started-on-demand HTTP inference server. Use as a context manager."""

    def __init__(
        self,
        *,
        name: str,
        argv: list[str],
        cwd: str | Path | None = None,
        env: dict | None = None,
        startup_timeout: float = 600.0,
        health_path: str = "/health",
        report: Callable[[str], None] | None = None,
        echo_log: bool = False,
    ) -> None:
        self.name = name
        self.argv = list(argv)
        self.cwd = str(cwd) if cwd else None
        self.env = env
        self.startup_timeout = startup_timeout
        self.health_path = health_path
        self.report = report or (lambda message: print(message, file=sys.stderr, flush=True))
        self.echo_log = echo_log
        self.port = 0
        self.process: subprocess.Popen | None = None
        self.log_path: Path | None = None
        self.load_seconds: float | None = None
        self._log_file = None
        self._job = None
        self._echo_stop = threading.Event()
        self._echo_thread: threading.Thread | None = None
        self._spill: SpillWatch | None = None

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.port = _free_port()
        self.log_path = _new_log_path(self.name)
        # Held open for the server's whole life (it is the child's stdout); closed in stop().
        self._log_file = open(self.log_path, "wb")  # noqa: SIM115
        argv = [*self.argv, "--host", "127.0.0.1", "--port", str(self.port)]
        self._log_file.write(("$ " + subprocess.list2cmdline(argv) + "\n").encode())
        self._log_file.flush()
        kwargs: dict = {
            "stdout": self._log_file,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "cwd": self.cwd,
            "env": self.env,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        started = time.monotonic()
        try:
            self.process = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            self._close_log()
            raise EngineError(f"Could not start {self.name}: {exc}", log_path=self.log_path) from exc
        with _live_lock:
            _live.add(self)
        if sys.platform == "win32":
            self._job = _bind_to_kill_on_close_job(self.process)
        # From the first moment: a model too big for the card spills while it loads.
        self._spill = SpillWatch(self.process.pid, label=self.name, report=self.report)
        self._spill.start()
        if self.echo_log:
            self._start_echo()
        try:
            self._wait_until_healthy(started)
        except BaseException:
            self.stop()
            raise
        self.load_seconds = time.monotonic() - started

    def stop(self) -> None:
        if self._spill is not None:
            self._spill.stop()   # last sample while the process still exists
        process, self.process = self.process, None
        with _live_lock:
            _live.discard(self)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
        if self._job is not None:
            _close_handle(self._job)
            self._job = None
        self._echo_stop.set()
        if self._echo_thread is not None:
            self._echo_thread.join(timeout=2)
        self._close_log()

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def spill_report(self) -> dict | None:
        """``{"vramSpillMb", ...}`` from the spill watchdog, or None where it cannot run (todo GPU-005)."""
        return self._spill.report_json() if self._spill is not None else None

    # -- requests ------------------------------------------------------------

    def get_json(self, path: str, timeout: float = 30.0) -> dict:
        body, _ = self._send(urllib.request.Request(self.base_url + path), timeout)
        return json.loads(body or b"{}")

    def post_json(self, path: str, payload: dict, *, timeout: float) -> tuple[bytes, dict]:
        request = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return self._send(request, timeout)

    def post_multipart(self, path: str, *, fields: dict[str, str], files: dict[str, Path],
                       timeout: float) -> tuple[bytes, dict]:
        boundary = f"----waterfree{uuid.uuid4().hex}"
        body = _multipart(boundary, fields, files)
        request = urllib.request.Request(
            self.base_url + path, data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return self._send(request, timeout)

    def _send(self, request: urllib.request.Request, timeout: float) -> tuple[bytes, dict]:
        if not self.running():
            raise self._crashed("is not running")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.read(), {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()
            raise EngineError(f"{self.name} rejected the request (HTTP {exc.code}): {detail}",
                              log_path=self.log_path, log_tail=self.log_tail(), status=exc.code) from exc
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
            if not self.running():
                raise self._crashed("crashed during the request") from exc
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise EngineError(f"{self.name} did not answer within {timeout:g} s.",
                                  log_path=self.log_path, log_tail=self.log_tail()) from exc
            raise EngineError(f"Request to {self.name} failed: {exc}",
                              log_path=self.log_path, log_tail=self.log_tail()) from exc

    # -- diagnostics -----------------------------------------------------------

    def log_tail(self, max_lines: int = 25) -> str:
        return read_log_tail(self.log_path, max_lines)

    def _crashed(self, what: str) -> EngineError:
        code = self.process.returncode if self.process is not None else None
        tail = self.log_tail()
        suffix = f" (exit code {code})" if code is not None else ""
        return EngineError(f"{self.name} {what}{suffix}. Log: {self.log_path}\n{tail}",
                           log_path=self.log_path, log_tail=tail)

    def _wait_until_healthy(self, started: float) -> None:
        deadline = started + self.startup_timeout
        next_report = started + 10.0
        while True:
            if self.process.poll() is not None:
                raise self._crashed("exited while loading")
            try:
                with urllib.request.urlopen(self.base_url + self.health_path, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            now = time.monotonic()
            if now >= deadline:
                raise EngineError(
                    f"{self.name} did not become ready within {self.startup_timeout:g} s. "
                    f"Log: {self.log_path}\n{self.log_tail()}",
                    log_path=self.log_path, log_tail=self.log_tail())
            if now >= next_report:
                self.report(f"loading {self.name}... {int(now - started)}s")
                next_report = now + 10.0
            time.sleep(0.5)

    def _start_echo(self) -> None:
        def follow() -> None:
            position = 0
            while not self._echo_stop.wait(1.0):
                try:
                    with open(self.log_path, "rb") as handle:
                        handle.seek(position)
                        chunk = handle.read()
                        position = handle.tell()
                except OSError:
                    continue
                for line in chunk.decode(errors="replace").splitlines():
                    if line.strip():
                        self.report(f"[{self.name}] {line.rstrip()}")

        self._echo_thread = threading.Thread(target=follow, daemon=True)
        self._echo_thread.start()

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            finally:
                self._log_file = None


def read_log_tail(path: Path | None, max_lines: int = 25) -> str:
    if path is None:
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 8192))
            text = handle.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-max_lines:])


def _stop_all() -> None:
    with _live_lock:
        servers = list(_live)
    for server in servers:
        try:
            server.stop()
        except Exception:  # noqa: BLE001, S110 - exit path; the Job Object is the backstop
            pass


atexit.register(_stop_all)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _new_log_path(name: str) -> Path:
    directory = engine_logs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    logs = sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime)
    for old in logs[:-_KEEP_LOGS]:
        try:
            old.unlink()
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return directory / f"{name}-{stamp}-{os.getpid()}.log"


def _multipart(boundary: str, fields: dict[str, str], files: dict[str, Path]) -> bytes:
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for key, path in files.items():
        path = Path(path)
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
             f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
            + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


# ---------------------------------------------------------------------------
# Windows Job Object: kill the server when this process goes away
# ---------------------------------------------------------------------------

def _bind_to_kill_on_close_job(process: subprocess.Popen):
    """Create a KILL_ON_JOB_CLOSE job and put ``process`` in it; return the job handle.

    Best effort: on failure the server still stops through the normal
    ``finally``/atexit paths, just not on a hard kill of this process.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x2000

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    ok = kernel32.SetInformationJobObject(job, job_object_extended_limit_information,
                                          ctypes.byref(info), ctypes.sizeof(info))
    if ok:
        ok = kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle)))
    if not ok:
        _close_handle(job)
        return None
    return job


def _close_handle(handle) -> None:
    if sys.platform != "win32" or not handle:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle(handle)
