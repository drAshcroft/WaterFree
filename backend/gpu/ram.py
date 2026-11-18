"""Free system RAM, and which programs hold it -- for jobs that run a big model on the CPU.

The GPU preflight (:mod:`backend.gpu.preflight`) guards VRAM. A few jobs also
put a large model in system RAM: animgen keeps Kimodo's 8B-parameter text
encoder on the CPU in bf16, ~15.5 GB resident. Started on a machine where that
much is not free, Windows does not fail it -- it pages other programs (the
editor, the browser, Unity) out to disk and the whole desktop stutters. So such
jobs check first and, when memory is short, name the programs worth closing.

No third-party packages: ``GlobalMemoryStatusEx`` and psapi via ctypes on
Windows, ``/proc`` on Linux. Anything that cannot be read is reported as
unknown rather than guessed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_GB = 1024 ** 3

# Processes a person cannot or should not close; listing them only hides the
# ones they can. Lower-case image names.
_UNCLOSABLE = frozenset((
    "system", "registry", "memory compression", "secure system", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe", "dwm.exe",
    "fontdrvhost.exe", "explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "textinputhost.exe", "runtimebroker.exe", "sihost.exe",
    "ctfmon.exe", "audiodg.exe", "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe",
    "spoolsv.exe", "wudfhost.exe", "conhost.exe", "taskhostw.exe", "dllhost.exe",
))


class InsufficientRam(RuntimeError):
    """Too little free system RAM for a job that loads a large model on the CPU."""

    def __init__(self, message: str, *, need_gb: float, available_gb: float, total_gb: float | None,
                 programs: list[dict]) -> None:
        super().__init__(message)
        self.need_gb = need_gb
        self.available_gb = available_gb
        self.total_gb = total_gb
        self.programs = programs

    def to_json(self) -> dict:
        return {
            "insufficientRam": True,
            "message": str(self),
            "needGb": round(self.need_gb, 1),
            "availableGb": round(self.available_gb, 1),
            "totalGb": round(self.total_gb, 1) if self.total_gb else None,
            "largestPrograms": self.programs,
        }


@dataclass(frozen=True)
class SystemMemory:
    total_gb: float
    available_gb: float

    def public(self) -> dict:
        return {"totalGb": round(self.total_gb, 1), "availableGb": round(self.available_gb, 1)}


def query() -> SystemMemory | None:
    """Total and available physical memory, or None if this platform cannot say.

    "Available" is what the OS can hand out without paging anything: on Windows
    ``ullAvailPhys`` (free + standby cache), on Linux ``MemAvailable``.
    """
    if sys.platform == "win32":
        return _query_windows()
    return _query_proc_meminfo()


def require(need_gb: float, *, purpose: str) -> SystemMemory | None:
    """Raise :class:`InsufficientRam` unless ``need_gb`` is available; return the reading."""
    memory = query()
    if memory is None or memory.available_gb >= need_gb:
        return memory
    programs = largest_programs()
    names = ", ".join(f"{p['name']} ({p['gb']:.1f} GB)" for p in programs[:5]) or "none could be read"
    raise InsufficientRam(
        f"{purpose} needs about {need_gb:.0f} GB of free RAM, but only {memory.available_gb:.1f} of "
        f"{memory.total_gb:.0f} GB is free. Starting anyway would push other programs out to disk and make "
        f"the whole machine stutter. The programs holding the most memory: {names}. Close what you can "
        "(a browser with many tabs, a game engine editor, extra editor windows, WSL) and retry, or pass "
        "--skip-ram-check to run anyway.",
        need_gb=need_gb, available_gb=memory.available_gb, total_gb=memory.total_gb, programs=programs,
    )


def largest_programs(limit: int = 8) -> list[dict]:
    """Programs by memory held, processes of one program summed: ``[{name, gb, processes}]``.

    Windows reports private commit (what closing the program frees); Linux
    reports resident set size. System processes and this process are left out.
    """
    totals: dict[str, list] = {}
    me = os.getpid()
    for pid, name, size in (_windows_processes() if sys.platform == "win32" else _proc_processes()):
        if pid == me or not name or name.lower() in _UNCLOSABLE:
            continue
        entry = totals.setdefault(name, [0, 0])
        entry[0] += size
        entry[1] += 1
    ranked = sorted(totals.items(), key=lambda item: item[1][0], reverse=True)[:limit]
    return [{"name": name, "gb": round(size / _GB, 2), "processes": count}
            for name, (size, count) in ranked if size >= _GB / 10]


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _query_windows() -> SystemMemory | None:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return SystemMemory(total_gb=status.ullTotalPhys / _GB, available_gb=status.ullAvailPhys / _GB)


def _windows_processes():
    """Yield ``(pid, image name, private bytes)`` for every process we may query."""
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD))

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
    process_query_limited_information = 0x1000

    capacity = 4096
    while True:
        pids = (wintypes.DWORD * capacity)()
        returned = wintypes.DWORD()
        if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(returned)):
            return
        count = returned.value // ctypes.sizeof(wintypes.DWORD)
        if count < capacity:
            break
        capacity *= 2
    buffer = ctypes.create_unicode_buffer(1024)
    for pid in pids[:count]:
        if not pid:
            continue
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            continue
        try:
            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                continue
            size = wintypes.DWORD(len(buffer))
            name = ""
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                name = Path(buffer.value).name
            yield int(pid), name, int(counters.PrivateUsage)
        finally:
            kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

def _query_proc_meminfo() -> SystemMemory | None:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                values[key] = int(parts[0]) * 1024
    except OSError:
        return None
    if "MemTotal" not in values or "MemAvailable" not in values:
        return None
    return SystemMemory(total_gb=values["MemTotal"] / _GB, available_gb=values["MemAvailable"] / _GB)


def _proc_processes():
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else ():
        if not entry.name.isdigit():
            continue
        try:
            fields = dict(line.split(":", 1) for line in (entry / "status").read_text().splitlines()
                          if ":" in line)
        except (OSError, ValueError):
            continue
        rss = fields.get("VmRSS", "").split()
        if rss and rss[0].isdigit():
            yield int(entry.name), fields.get("Name", "").strip(), int(rss[0]) * 1024
