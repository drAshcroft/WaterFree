"""Free/total VRAM and the processes using the card, via ``nvidia-smi``.

``nvidia-smi`` ships with the driver, so this needs no Python GPU package and
works in a process that never imports torch. Under Windows WDDM it cannot
attribute memory to individual processes (it prints ``[N/A]``), so callers get
the *names* of the other GPU users but only a card-wide figure for their use.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class GpuMemory:
    index: int
    name: str
    total_mb: int
    used_mb: int
    free_mb: int

    @property
    def free_gb(self) -> float:
        return self.free_mb / 1024

    @property
    def total_gb(self) -> float:
        return self.total_mb / 1024

    def public(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "totalGb": round(self.total_mb / 1024, 2),
            "usedGb": round(self.used_mb / 1024, 2),
            "freeGb": round(self.free_mb / 1024, 2),
        }


def nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "nvidia-smi.exe"
    return str(candidate) if candidate.is_file() else None


def _run(args: list[str]) -> list[str] | None:
    binary = nvidia_smi()
    if not binary:
        return None
    try:
        result = subprocess.run([binary, *args], capture_output=True, text=True, check=False,
                                timeout=_TIMEOUT_SECONDS, creationflags=_no_window())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def query(index: int = 0) -> GpuMemory | None:
    """Memory on GPU ``index``, or None when there is no NVIDIA GPU or no driver tool."""
    lines = _run(["--query-gpu=index,name,memory.total,memory.used,memory.free",
                  "--format=csv,noheader,nounits"])
    for line in lines or []:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpu = GpuMemory(int(parts[0]), parts[1], int(parts[2]), int(parts[3]), int(parts[4]))
        except ValueError:
            continue
        if gpu.index == index:
            return gpu
    return None


# Windows shell hosts that always hold a small GPU context. Naming them when
# VRAM is short only buries the programs a person could actually close.
_SHELL_PROCESSES = frozenset(name.lower() for name in (
    "explorer.exe", "dwm.exe", "SearchHost.exe", "StartMenuExperienceHost.exe",
    "ShellExperienceHost.exe", "ShellHost.exe", "TextInputHost.exe", "LockApp.exe",
    "CrossDeviceResume.exe", "PhoneExperienceHost.exe", "Widgets.exe", "SystemSettings.exe",
))


def significant_process_names(processes: list[dict]) -> list[str]:
    """Distinct names of GPU users worth mentioning, shell hosts left out."""
    return sorted({p["name"] for p in processes if p["name"] and p["name"].lower() not in _SHELL_PROCESSES},
                  key=str.lower)


def gpu_processes() -> list[dict]:
    """Processes with a context on the GPU: ``[{pid, name, usedMb|None}]``."""
    lines = _run(["--query-compute-apps=pid,process_name,used_memory",
                  "--format=csv,noheader,nounits"])
    out = []
    for line in lines or []:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        name = parts[1]
        if name.startswith("["):  # "[Insufficient Permissions]"
            name = ""
        used = int(parts[2]) if parts[2].isdigit() else None
        out.append({"pid": pid, "name": Path(name).name if name else "", "usedMb": used})
    return out
