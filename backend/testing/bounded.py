"""
Bounded subprocess execution shared by every test runner.

Why this exists: a GDScript parse error can leave a headless Godot run alive
and silent instead of exiting. The only thing that ended it was the caller's
own tool timeout, minutes later and with no diagnosis (KB a264a8f4). The same
thing happens to any runner whose process can wedge: a vitest worker waiting
on a port, a dotnet build blocked on a lock.

`run_bounded` replaces `subprocess.run` for all of them and adds:

* **budget**: a wall-time limit for the whole *run*. The deadline is shared by
  every process one runner spawns, so 48 npm scripts or two dotnet targets
  get one budget between them, not one each.
* **stall**: a limit on time without any stdout/stderr output.
* **a real process-tree kill**: `taskkill /T /F` plus a Windows Job Object
  (which also catches descendants whose parent already exited); a process
  group on POSIX. `subprocess.run(timeout=)` only kills the direct child,
  which leaves Godot or node running behind a `.cmd` shim, holding the pipes
  open, and writing to a log long after the caller has given up on it.
* **first script error**: the first Godot `SCRIPT ERROR` / `Parse Error` line
  and its `file:line`, so an incomplete result says *why* the run hung.

Budget/stall expiry raises RunIncompleteError, which the CLI maps to exit 4
(no verdict). A runner's own legacy timeout, used when no budget is given,
still raises subprocess.TimeoutExpired so each runner keeps its documented
timeout behaviour. It now kills the whole tree too.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO

from backend.testing.errors import RunIncompleteError

# How long to keep reading after the main process exits. Output still in the
# pipe drains in milliseconds; this only matters when a grandchild inherited
# the pipe and outlives its parent (a build server, a spawned dev server).
# `subprocess.run` waits for that forever. We stop after this long.
_DRAIN_SECONDS = 5.0

POLL_INTERVAL_SECONDS = 0.1


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------

_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)?")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(text: str) -> float:
    """Parse `90`, `90s`, `5m`, `1h30m`, `1.5h` or `250ms` into seconds.

    A bare number means seconds, but only on its own: `1h30` is rejected
    rather than guessed at. Zero and negative durations are rejected because
    every caller uses a duration as a limit, and a zero limit is a typo.
    """
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if not compact:
        raise ValueError("duration is empty; use e.g. 90s, 5m or 1h30m")
    tokens = _DURATION_TOKEN.findall(compact)
    rebuilt = "".join(number + unit for number, unit in tokens)
    if rebuilt != compact or (len(tokens) > 1 and any(not unit for _, unit in tokens)):
        raise ValueError(
            f"invalid duration {text!r}; use a number of seconds or units "
            "h/m/s/ms, e.g. 90s, 5m, 1h30m"
        )
    total = sum(float(number) * _UNIT_SECONDS[unit or "s"] for number, unit in tokens)
    if total <= 0:
        raise ValueError(f"duration {text!r} must be greater than zero")
    return total


def format_seconds(value: float) -> str:
    """Compact human form for messages: 3s, 2.5s, 5m, 1h30m."""
    if value < 60:
        return f"{value:.3g}s"
    minutes, seconds = divmod(round(value), 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours}h" if hours else "", f"{minutes}m" if minutes else "",
             f"{seconds}s" if seconds else ""]
    return "".join(parts) or "0s"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


@dataclass
class RunLimits:
    """Budget and stall limits for one runner invocation.

    Created by the CLI immediately before the run starts; `started` anchors the
    budget so every process the runner spawns draws on the same deadline.
    """

    budget: float | None = None
    stall: float | None = None
    started: float = field(default_factory=time.monotonic)

    @property
    def active(self) -> bool:
        return bool(self.budget or self.stall)

    def deadline(self) -> float | None:
        return self.started + self.budget if self.budget else None

    def elapsed(self) -> float:
        return time.monotonic() - self.started


# ---------------------------------------------------------------------------
# First script error
# ---------------------------------------------------------------------------

# Godot 4 prints `SCRIPT ERROR: Parse Error: ...` followed by an `at:` line
# carrying `(res://path.gd:12)`; Godot 3 printed `Parser Error:`. Case-sensitive
# on purpose: `Failed to load script ... with error "Parse error"` is a
# consequence of the real error, which has already been printed above it.
_SCRIPT_ERROR_RE = re.compile(r"SCRIPT ERROR|Parse Error|Parser Error")
_LOCATION_RE = re.compile(
    r"((?:res|user)://[^\s()'\"]+?:\d+"
    r"|[A-Za-z]:[\\/][^\s()'\"]+?:\d+"
    r"|[\w./\\-]+\.(?:gd|cs|py|ts|js|mjs):\d+)"
)


def first_script_error(*texts: str) -> tuple[str | None, str | None]:
    """The first script/parse error line and its `file:line`, if any.

    Texts are searched in the order given; pass stderr first, which is where
    Godot writes its errors.
    """
    for text in texts:
        lines = (text or "").splitlines()
        for index, line in enumerate(lines):
            if not _SCRIPT_ERROR_RE.search(line):
                continue
            location = None
            # The location is on the same line (Godot 3) or within the next
            # couple of `at:` lines (Godot 4).
            for candidate in lines[index:index + 4]:
                match = _LOCATION_RE.search(candidate)
                if match:
                    location = match.group(1)
                    break
            return line.strip(), location
    return None, None


# ---------------------------------------------------------------------------
# Process-tree kill
# ---------------------------------------------------------------------------


_CREATE_SUSPENDED = 0x00000004


def _spawn(argv: list[str], popen_kwargs: dict) -> tuple[subprocess.Popen, _WindowsJob | None]:
    """Start the process; on Windows, inside a Job Object from its first instruction.

    The process is created suspended, put in the job, then resumed. Assigning
    the job after a normal start races the child: a venv `python.exe` launcher
    or a `.cmd` shim starts its real child within milliseconds, and that child
    (and everything it spawns) would sit outside the job. If the job or the
    resume fails, the never-run suspended process is killed and the command is
    started plainly — taskkill /T still covers that case.
    """
    if os.name != "nt":
        return subprocess.Popen(argv, **popen_kwargs), None

    flags = popen_kwargs.get("creationflags", 0)
    proc = subprocess.Popen(argv, **{**popen_kwargs, "creationflags": flags | _CREATE_SUSPENDED})
    job = _WindowsJob.attach(proc.pid)
    if job is not None and _resume_process(proc.pid):
        return proc, job

    # Never leave a suspended process behind.
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    if job is not None:
        job.close()
    return subprocess.Popen(argv, **popen_kwargs), None


def _resume_process(pid: int) -> bool:
    """Resume every thread of a process created with CREATE_SUSPENDED."""
    try:
        import ctypes
        from ctypes import wintypes

        class _ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        th32cs_snapthread = 0x00000004
        thread_suspend_resume = 0x0002
        invalid_handle = wintypes.HANDLE(-1).value

        snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapthread, 0)
        if not snapshot or snapshot == invalid_handle:
            return False
        resumed = 0
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(_ThreadEntry)
            more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = kernel32.OpenThread(thread_suspend_resume, False, entry.th32ThreadID)
                    if thread:
                        try:
                            if kernel32.ResumeThread(thread) != 0xFFFFFFFF:
                                resumed += 1
                        finally:
                            kernel32.CloseHandle(thread)
                more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return resumed > 0
    except (OSError, AttributeError, ValueError):
        return False


class _ProcessTree:
    """Owns the kill for one spawned process and everything it starts."""

    def __init__(self, proc: subprocess.Popen, job: _WindowsJob | None = None) -> None:
        self.proc = proc
        self._job = job

    def kill(self) -> None:
        if os.name == "nt":
            # taskkill walks the live parent chain; the job catches anything
            # whose parent has already exited. Either alone has a gap.
            _taskkill_tree(self.proc.pid)
            if self._job is not None:
                self._job.terminate()
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None


def _taskkill_tree(pid: int) -> None:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    taskkill = os.path.join(system_root, "System32", "taskkill.exe")
    try:
        subprocess.run(
            [taskkill, "/T", "/F", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        pass


class _WindowsJob:
    """A Job Object holding the spawned process and, by inheritance, its tree.

    Deliberately *not* KILL_ON_JOB_CLOSE: a build server or daemon that a
    successful run leaves behind (MSBuild nodes, VBCSCompiler) is meant to
    persist, and killing it on every run would make the next build slower. The
    job is only used to terminate the tree when we decide to stop the run.
    Breakaway is allowed so a child that asks for CREATE_BREAKAWAY_FROM_JOB
    still starts instead of failing with access denied.
    """

    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
    _JOB_OBJECT_BASIC_LIMIT_INFORMATION_CLASS = 2

    def __init__(self, kernel32, handle) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    @classmethod
    def attach(cls, pid: int) -> _WindowsJob | None:
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:  # pragma: no cover - ctypes always ships on Windows
            return None

        class _BasicLimits(ctypes.Structure):
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

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            limits = _BasicLimits()
            limits.LimitFlags = cls._JOB_OBJECT_LIMIT_BREAKAWAY_OK
            kernel32.SetInformationJobObject(
                job, cls._JOB_OBJECT_BASIC_LIMIT_INFORMATION_CLASS,
                ctypes.byref(limits), ctypes.sizeof(limits),
            )
            process = kernel32.OpenProcess(
                cls._PROCESS_SET_QUOTA | cls._PROCESS_TERMINATE, False, pid,
            )
            if not process:
                kernel32.CloseHandle(job)
                return None
            try:
                assigned = kernel32.AssignProcessToJobObject(job, process)
            finally:
                kernel32.CloseHandle(process)
            if not assigned:
                kernel32.CloseHandle(job)
                return None
            return cls(kernel32, job)
        except (OSError, AttributeError, ValueError):
            return None

    def terminate(self) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------


class _Activity:
    """Monotonic time of the last byte seen on either pipe."""

    __slots__ = ("last",)

    def __init__(self) -> None:
        self.last = time.monotonic()


def _pump(pipe: IO[bytes], sink: list[bytes], activity: _Activity) -> None:
    read = getattr(pipe, "read1", None) or pipe.read
    try:
        while True:
            chunk = read(65536)
            if not chunk:
                break
            sink.append(chunk)
            activity.last = time.monotonic()
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _decode(chunks: list[bytes]) -> str:
    # Universal newlines, as `text=True` would give the parsers.
    return b"".join(list(chunks)).decode("utf-8", errors="replace").replace("\r\n", "\n")


def run_bounded(
    cmd: Sequence[str],
    *,
    cwd: str | None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    limits: RunLimits | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `cmd` to completion under the given limits.

    * `limits.budget` set: the run's shared deadline is the only wall-time
      limit (it replaces `timeout`, so a budget can be longer than a runner's
      default); expiry raises RunIncompleteError(reason="budget").
    * otherwise `timeout` applies and raises subprocess.TimeoutExpired, with
      the partial output attached, after killing the tree.
    * `limits.stall` set: that long without output raises
      RunIncompleteError(reason="stall").

    Spawn failures propagate as OSError for the caller to translate into its
    own setup error. stdin is always closed: a runner waiting for input would
    otherwise hang exactly the way this module exists to prevent.
    """
    argv = [str(part) for part in cmd]
    budget_deadline = limits.deadline() if limits else None
    stall = limits.stall if limits else None

    hard_deadline: float | None = None
    hard_kind = "timeout"
    if budget_deadline is not None:
        hard_deadline, hard_kind = budget_deadline, "budget"
        if time.monotonic() >= budget_deadline:
            # An earlier process of the same run used the whole budget.
            raise _incomplete(argv, "budget", limits, "", "", spawned=False)
    elif timeout:
        hard_deadline = time.monotonic() + float(timeout)

    popen_kwargs: dict = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": {**os.environ, **env} if env else None,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    proc, job = _spawn(argv, popen_kwargs)
    tree = _ProcessTree(proc, job)
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    activity = _Activity()
    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, out_chunks, activity), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err_chunks, activity), daemon=True),
    ]
    for reader in readers:
        reader.start()

    reason: str | None = None
    try:
        while True:
            try:
                proc.wait(timeout=POLL_INTERVAL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if hard_deadline is not None and now >= hard_deadline:
                reason = hard_kind
                break
            if stall and now - activity.last >= stall:
                reason = "stall"
                break
    except BaseException:
        # Ctrl+C or anything unexpected: never leave the tree running.
        tree.kill()
        tree.close()
        raise

    if reason:
        tree.kill()
    drain_until = time.monotonic() + _DRAIN_SECONDS
    for reader in readers:
        reader.join(max(0.0, drain_until - time.monotonic()))
    tree.close()

    stdout = _decode(out_chunks)
    stderr = _decode(err_chunks)
    if reason == "timeout":
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
    if reason:
        raise _incomplete(argv, reason, limits, stdout, stderr)
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _incomplete(
    argv: list[str], reason: str, limits: RunLimits | None, stdout: str, stderr: str,
    *, spawned: bool = True,
) -> RunIncompleteError:
    elapsed = limits.elapsed() if limits else 0.0
    limit = (limits.budget if reason == "budget" else limits.stall) if limits else None
    first_error, location = first_script_error(stderr, stdout)
    if not spawned:
        stopped = (
            f"the {format_seconds(limit or 0)} wall-time budget (--budget) was used "
            "up by earlier steps of this run, so the next step was not launched"
        )
    elif reason == "budget":
        stopped = (
            f"the test process exceeded the {format_seconds(limit or 0)} wall-time "
            "budget (--budget), so its process tree was killed"
        )
    else:
        stopped = (
            f"the test process printed nothing for {format_seconds(limit or 0)} "
            "(--stall), so its process tree was killed"
        )
    message = (
        f"Run stopped after {format_seconds(elapsed)}: {stopped}. This is not a "
        "pass and not a red suite: no verdict was produced."
    )
    if first_error:
        message += f" First script error: {first_error}"
        if location and location not in first_error:
            message += f" ({location})"
        message += "."
    message += " Partial output: `waterfree testing logs`."
    raw = (
        f"$ {' '.join(argv)}\n\n{stdout}{stderr}\n"
        f"[waterfree] {reason}: {message}\n"
    )
    return RunIncompleteError(
        message,
        reason=reason,
        elapsed=elapsed,
        limit=limit,
        first_error=first_error,
        first_error_location=location,
        raw_output=raw,
    )


def pid_alive(pid: int) -> bool:
    """True while process `pid` exists and has not exited.

    Used by `testing await --pid` and by the tests that check a killed tree is
    really gone. No psutil: it is not a dependency of this project.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # ERROR_ACCESS_DENIED means it exists but belongs to someone else.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
