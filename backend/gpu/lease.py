"""Machine-wide GPU lease: one exclusive holder, or any number of shared ones.

Why this exists
---------------
A 12 GB card holds one large model at a time, and on Windows the desktop already
uses ~2 GB of it. WaterFree has several independent GPU users -- imagegen
(diffusers), meshgen (trellis.cpp), audiogen (openmoss), and every Ollama-backed
area (vision, qa, qa-summary, writing-grade, ...) -- each started by a separate
CLI process, often by different agents at the same time. Without coordination
the second job either fails with an out-of-memory error or, worse, the NVIDIA
Windows driver silently spills the allocation into system RAM and the job runs
ten times slower with no error at all.

Model
-----
* **exclusive** -- heavy jobs that load their own model (imagegen, meshgen,
  audiogen). One holder, nobody else.
* **shared** -- Ollama clients. Ollama schedules its own models, so any number
  of them may hold the lease together; they only have to stay out of the way of
  exclusive jobs.
* **FIFO queue.** A request is granted when nothing incompatible is held *and*
  nothing incompatible is queued ahead of it, so a stream of shared requests
  cannot starve a waiting exclusive job, and vice versa.

State lives in ``state.json`` under :func:`backend.app_paths.gpu_state_dir`,
guarded by a short-held OS file lock. Every entry records its process id *and
that process's creation time*; an entry whose process is gone (or whose pid has
been reused by a different process) is pruned on the next read. That is the
crash-safety guarantee: a killed holder never blocks the GPU for longer than
the next poll.

A process holds at most one lease. Nested requests that the held lease already
covers (shared under exclusive, or a second exclusive) just bump a reference
count. A child process started by a holder can inherit its lease through
``WATERFREE_GPU_LEASE`` (see :func:`child_env`) instead of deadlocking on it.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Self

from backend.app_paths import gpu_state_dir

SHARED = "shared"
EXCLUSIVE = "exclusive"
MODES: tuple[str, ...] = (SHARED, EXCLUSIVE)

LEASE_ENV = "WATERFREE_GPU_LEASE"
"""Id of a lease held by an ancestor process; the child reuses it."""

DISABLE_ENV = "WATERFREE_GPU_SCHEDULER"
"""Set to ``0`` to turn the scheduler off (remote-GPU setups, unrelated tests)."""

PROGRESS_ENV = "WATERFREE_GPU_PROGRESS"


def default_wait_timeout() -> float:
    try:
        return max(0.0, float(os.environ.get("WATERFREE_GPU_WAIT_TIMEOUT", "1800")))
    except ValueError:
        return 1800.0


_STATE_FILE = "state.json"
_MUTEX_FILE = "state.lock"
_ALIVE_UNVERIFIED = -1  # the process exists but its identity cannot be read
_REPORT_EVERY_SECONDS = 15.0


class GpuBusy(RuntimeError):
    """The lease could not be granted before the wait policy ran out."""

    def __init__(self, message: str, *, holders: list[dict], queue: list[dict], waited: float) -> None:
        super().__init__(message)
        self.holders = holders
        self.queue = queue
        self.waited = waited

    def to_json(self) -> dict:
        return {
            "gpuBusy": True,
            "message": str(self),
            "waitedSeconds": round(self.waited, 1),
            "holders": self.holders,
            "queue": self.queue,
        }


@dataclass
class WaitPolicy:
    """How long to queue for the GPU. ``timeout == 0`` means "try once, don't wait"."""

    timeout: float = field(default_factory=default_wait_timeout)
    progress: bool = True
    report: Callable[[str], None] | None = None

    def emit(self, message: str) -> None:
        if not self.progress or os.environ.get(PROGRESS_ENV, "1") == "0":
            return
        (self.report or _stderr)(message)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass
class Entry:
    id: str
    pid: int
    token: int | None          # process creation time, opaque; guards against pid reuse
    mode: str
    label: str                 # "meshgen make", "vision look", ...
    detail: str = ""           # "trellis2 q8, res 1024, 3 images"
    vram_gb: float | None = None
    queued_at: float = 0.0
    granted_at: float | None = None

    def public(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        out = {
            "id": self.id,
            "pid": self.pid,
            "mode": self.mode,
            "label": self.label,
            "detail": self.detail,
            "vramGb": self.vram_gb,
            "queuedAt": _iso(self.queued_at),
        }
        if self.granted_at is not None:
            out["grantedAt"] = _iso(self.granted_at)
            out["heldSeconds"] = round(now - self.granted_at, 1)
        else:
            out["waitingSeconds"] = round(now - self.queued_at, 1)
        return out


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


# ---------------------------------------------------------------------------
# Process identity
# ---------------------------------------------------------------------------

def process_token(pid: int) -> int | None:
    """An opaque identity for a live process, or None when ``pid`` is not running.

    Returns ``_ALIVE_UNVERIFIED`` when the process exists but its creation time
    cannot be read (another user's process, for instance).
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _win_process_token(pid)
    return _posix_process_token(pid)


def _win_process_token(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4

    process_query_limited_information = 0x1000
    error_access_denied = 5
    still_active = 259

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return _ALIVE_UNVERIFIED if ctypes.get_last_error() == error_access_denied else None
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != still_active:
            return None  # exited; someone still holds a handle to it
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return _ALIVE_UNVERIFIED
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_token(pid: int) -> int | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return _ALIVE_UNVERIFIED
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 (starttime); fields after the ")" that closes comm start at field 3.
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return _ALIVE_UNVERIFIED


def _is_alive(entry: Entry) -> bool:
    current = process_token(entry.pid)
    if current is None:
        return False
    if current == _ALIVE_UNVERIFIED or entry.token in (None, _ALIVE_UNVERIFIED):
        return True
    return current == entry.token


# ---------------------------------------------------------------------------
# Shared state file
# ---------------------------------------------------------------------------

class _FileMutex:
    """A short-held, cross-process exclusive lock on one byte of a file."""

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self._fh = None

    def __enter__(self) -> Self:
        self._fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    raise TimeoutError(f"GPU state lock {self.path} stayed busy for {self.timeout:g} s")
                time.sleep(0.02)

    def __exit__(self, *_exc) -> None:
        try:
            self._unlock()
        finally:
            self._fh.close()

    def _lock(self) -> None:
        if sys.platform == "win32":
            import msvcrt
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if sys.platform == "win32":
            import msvcrt
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)


@dataclass
class _State:
    holders: list[Entry] = field(default_factory=list)
    queue: list[Entry] = field(default_factory=list)

    def find(self, entry_id: str) -> Entry | None:
        return next((e for e in self.holders if e.id == entry_id), None)


def _load(path: Path) -> _State:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _State()
    except (OSError, ValueError):
        # A torn or hand-edited file must not wedge every GPU user; every live
        # holder re-checks its own entry, so starting over is safe.
        return _State()
    fields = set(Entry.__dataclass_fields__)

    def entries(key: str) -> list[Entry]:
        out = []
        for item in raw.get(key, []) if isinstance(raw, dict) else []:
            if isinstance(item, dict):
                try:
                    out.append(Entry(**{k: v for k, v in item.items() if k in fields}))
                except TypeError:
                    continue
        return out

    return _State(holders=entries("holders"), queue=entries("queue"))


def _save(path: Path, state: _State) -> None:
    body = json.dumps({
        "version": 1,
        "holders": [asdict(e) for e in state.holders],
        "queue": [asdict(e) for e in state.queue],
    }, indent=2)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: a scanner or a reader without the mutex can hold the
            # target open for a moment.
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


_THREAD_LOCK = threading.RLock()


@contextmanager
def _locked_state() -> Iterator[_State]:
    """Load, prune and (if changed) save the state under the cross-process mutex."""
    directory = gpu_state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _STATE_FILE
    with _THREAD_LOCK, _FileMutex(directory / _MUTEX_FILE):
        state = _load(path)
        before = _fingerprint(state)
        state.holders = [e for e in state.holders if _is_alive(e)]
        state.queue = [e for e in state.queue if _is_alive(e)]
        yield state
        if _fingerprint(state) != before:
            _save(path, state)


def _fingerprint(state: _State) -> str:
    return json.dumps([[asdict(e) for e in state.holders], [asdict(e) for e in state.queue]],
                      sort_keys=True)


def _grantable(state: _State, ticket: Entry) -> bool:
    ahead = []
    for queued in state.queue:
        if queued.id == ticket.id:
            break
        ahead.append(queued)
    if ticket.mode == EXCLUSIVE:
        return not state.holders and not ahead
    return (not any(h.mode == EXCLUSIVE for h in state.holders)
            and not any(q.mode == EXCLUSIVE for q in ahead))


# ---------------------------------------------------------------------------
# This process's lease
# ---------------------------------------------------------------------------

@dataclass
class _Held:
    entry: Entry
    refs: int = 0
    process_scoped: bool = False   # held until exit (lazy Ollama use in a CLI command)
    inherited: bool = False        # owned by an ancestor process; never written to state


_held: _Held | None = None


def scheduler_enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "1").strip() not in {"0", "false", "off", "no"}


class Lease:
    """A handle on this process's lease. Release it, or use it as a context manager."""

    def __init__(self, mode: str, entry: Entry | None) -> None:
        self.mode = mode
        self.entry = entry
        self._released = False

    @property
    def id(self) -> str | None:
        return self.entry.id if self.entry else None

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.entry is not None:
            _drop_ref()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def acquire(
    mode: str,
    *,
    label: str,
    detail: str = "",
    vram_gb: float | None = None,
    policy: WaitPolicy | None = None,
) -> Lease:
    """Queue for the GPU and return once ``mode`` is granted.

    Raises :class:`GpuBusy` when ``policy.timeout`` runs out first. The queue
    ticket is withdrawn on any exception, including KeyboardInterrupt.
    """
    global _held
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if not scheduler_enabled():
        return Lease(mode, None)
    policy = policy or WaitPolicy()

    with _THREAD_LOCK:
        if _held is None:
            _held = _inherit(mode)
        if _held is not None:
            if _held.entry.mode == EXCLUSIVE or mode == SHARED:
                _held.refs += 1
                return Lease(mode, _held.entry)
            if _held.refs > 0 or _held.inherited:
                raise RuntimeError(
                    "This process already holds a shared GPU lease in use; it cannot "
                    "upgrade to exclusive without releasing it first."
                )
            _withdraw(_held.entry.id)   # a lazily-held shared lease: give it up and queue
            _held = None

        entry = _new_entry(mode, label=label, detail=detail, vram_gb=vram_gb)
        _wait_for_grant(entry, policy)
        _held = _Held(entry=entry, refs=1)
        return Lease(mode, entry)


@contextmanager
def hold(mode: str, **kwargs) -> Iterator[Lease]:
    lease = acquire(mode, **kwargs)
    try:
        yield lease
    finally:
        lease.release()


def ensure_process_shared(*, label: str, detail: str = "", policy: WaitPolicy | None = None) -> None:
    """Hold a shared lease from now until this process exits (or upgrades).

    For one-shot CLI commands that talk to Ollama: taking it once per command
    rather than per request keeps a heavy job from slipping in between two
    requests and evicting the model the command is in the middle of using.
    """
    global _held
    if not scheduler_enabled():
        return
    with _THREAD_LOCK:
        if _held is not None:
            return
        inherited = _inherit(SHARED)
        if inherited is not None:
            inherited.process_scoped = True
            _held = inherited
            return
        entry = _new_entry(SHARED, label=label, detail=detail, vram_gb=None)
        _wait_for_grant(entry, policy or WaitPolicy())
        _held = _Held(entry=entry, refs=0, process_scoped=True)


def held_lease_id() -> str | None:
    with _THREAD_LOCK:
        return _held.entry.id if _held else None


def child_env(base: dict | None = None) -> dict:
    """An environment for a child process that should reuse this process's lease."""
    env = dict(os.environ if base is None else base)
    lease_id = held_lease_id()
    if lease_id:
        env[LEASE_ENV] = lease_id
    return env


def release_all() -> None:
    """Drop this process's lease regardless of reference counts (atexit)."""
    global _held
    with _THREAD_LOCK:
        if _held is not None and not _held.inherited:
            try:
                _withdraw(_held.entry.id)
            except Exception:  # noqa: BLE001, S110 - exit path; liveness pruning covers us
                pass
        _held = None


atexit.register(release_all)


def snapshot() -> dict:
    """Live holders and waiters, oldest first, for `waterfree gpu status`."""
    if not scheduler_enabled():
        return {"enabled": False, "holders": [], "queue": []}
    with _locked_state() as state:
        now = time.time()
        return {
            "enabled": True,
            "stateDir": str(gpu_state_dir()),
            "holders": [e.public(now) for e in state.holders],
            "queue": [e.public(now) for e in state.queue],
        }


def clear_stale() -> dict:
    """Prune dead holders/waiters now; returns what remained. Pruning also happens on every read."""
    return snapshot()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _new_entry(mode: str, *, label: str, detail: str, vram_gb: float | None) -> Entry:
    pid = os.getpid()
    return Entry(
        id=uuid.uuid4().hex[:12],
        pid=pid,
        token=process_token(pid),
        mode=mode,
        label=label or _default_label(),
        detail=detail,
        vram_gb=vram_gb,
        queued_at=time.time(),
    )


_command_label = ""


def set_command_label(label: str) -> None:
    """Name this process's GPU use in `gpu status` (the dispatcher sets "<area> <action>")."""
    global _command_label
    _command_label = label.strip()


def _default_label() -> str:
    if _command_label:
        return _command_label
    args = [a for a in sys.argv[1:3] if not a.startswith("-")]
    return " ".join(args) or Path(sys.argv[0]).name


def _inherit(mode: str) -> _Held | None:
    lease_id = os.environ.get(LEASE_ENV, "").strip()
    if not lease_id:
        return None
    with _locked_state() as state:
        entry = state.find(lease_id)
    if entry is None or entry.pid == os.getpid():
        return None
    if entry.mode == SHARED and mode == EXCLUSIVE:
        return None
    return _Held(entry=entry, refs=0, inherited=True)


def _drop_ref() -> None:
    global _held
    with _THREAD_LOCK:
        if _held is None:
            return
        _held.refs = max(0, _held.refs - 1)
        if _held.refs == 0 and not _held.process_scoped:
            if not _held.inherited:
                _withdraw(_held.entry.id)
            _held = None


def _withdraw(entry_id: str) -> None:
    with _locked_state() as state:
        state.holders = [e for e in state.holders if e.id != entry_id]
        state.queue = [e for e in state.queue if e.id != entry_id]


def _wait_for_grant(entry: Entry, policy: WaitPolicy) -> None:
    started = time.monotonic()
    deadline = started + policy.timeout
    next_report = started
    poll = 0.2
    with _locked_state() as state:
        state.queue.append(entry)
    try:
        while True:
            with _locked_state() as state:
                if _grantable(state, entry):
                    state.queue = [e for e in state.queue if e.id != entry.id]
                    entry.granted_at = time.time()
                    state.holders.append(entry)
                    if time.monotonic() - started > 1.0:
                        policy.emit(f"GPU acquired ({entry.mode}) after "
                                    f"{_fmt(time.monotonic() - started)}.")
                    return
                holders = list(state.holders)
                position = next((i for i, q in enumerate(state.queue) if q.id == entry.id), 0) + 1
                queue_len = len(state.queue)
                queue_public = [q.public() for q in state.queue]

            now = time.monotonic()
            if now >= deadline:
                waited = now - started
                raise GpuBusy(
                    _busy_message(entry, holders, waited, policy.timeout),
                    holders=[h.public() for h in holders],
                    queue=[q for q in queue_public if q["id"] != entry.id],
                    waited=waited,
                )
            if now >= next_report:
                policy.emit(_waiting_message(entry, holders, position, queue_len, now - started))
                # Every 15 s for the first minute, then once a minute: a mesh
                # job can hold the GPU for ten minutes and stderr is not a log.
                next_report = now + (_REPORT_EVERY_SECONDS if now - started < 60 else 60.0)
            time.sleep(max(0.0, min(poll, deadline - now)))
            poll = min(1.0, poll * 1.5)
    except BaseException:
        _withdraw(entry.id)
        raise


def _describe(entry: Entry) -> str:
    held = f", {_fmt(time.time() - entry.granted_at)} in" if entry.granted_at else ""
    detail = f" [{entry.detail}]" if entry.detail else ""
    return f"{entry.label} ({entry.mode}, pid {entry.pid}{held}){detail}"


def _waiting_message(entry: Entry, holders: list[Entry], position: int, queue_len: int,
                     waited: float) -> str:
    holder_text = "; ".join(_describe(h) for h in holders) or "a job queued ahead"
    return (f"waiting for GPU ({entry.mode}, {entry.label}): held by {holder_text}; "
            f"you are #{position} of {queue_len} in the queue, waited {_fmt(waited)}")


def _busy_message(entry: Entry, holders: list[Entry], waited: float, timeout: float) -> str:
    holder_text = "; ".join(_describe(h) for h in holders) or "jobs queued ahead"
    if timeout <= 0:
        return f"GPU is busy: {holder_text}. Not waiting (--no-wait)."
    return (f"GPU still busy after waiting {_fmt(waited)}: {holder_text}. "
            "Raise --wait-timeout, or run `waterfree gpu status` to see the queue.")


def _fmt(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
