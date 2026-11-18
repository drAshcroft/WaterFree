"""BVH to FBX through a headless Blender, for engines that cannot import BVH (Unity)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("blender_bvh_to_fbx.py")
_RESULT = "WATERFREE_RESULT "
_STARTUP_SECONDS = 120
_PER_CLIP_SECONDS = 60


def find_blender(configured: str = "") -> Path | None:
    """Blender from config, $WATERFREE_BLENDER, PATH, then the newest standard Windows install."""
    for candidate in (configured, os.environ.get("WATERFREE_BLENDER", "")):
        if candidate.strip() and Path(candidate).expanduser().is_file():
            return Path(candidate).expanduser()
    found = shutil.which("blender")
    if found:
        return Path(found)
    if sys.platform == "win32":
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"
        installs = [p / "blender.exe" for p in root.glob("Blender*") if (p / "blender.exe").is_file()]
        if installs:
            return max(installs, key=lambda p: _version_key(p.parent.name))
    return None


def _version_key(folder_name: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", folder_name)) or (0,)


def convert(jobs: list[dict], blender: Path) -> list[dict]:
    """Run one Blender for the whole batch; return one result per job (``ok`` or ``error``)."""
    if not jobs:
        return []
    with tempfile.TemporaryDirectory(prefix="waterfree-fbx-") as folder:
        jobs_path = Path(folder) / "jobs.json"
        jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
        try:
            completed = subprocess.run(
                [str(blender), "-b", "--factory-startup", "--python", str(SCRIPT), "--", str(jobs_path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                timeout=_STARTUP_SECONDS + _PER_CLIP_SECONDS * len(jobs),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [{**job, "ok": False, "error": f"Blender did not finish: {exc}"} for job in jobs]
    results = {}
    for line in completed.stdout.splitlines():
        if line.startswith(_RESULT):
            try:
                item = json.loads(line[len(_RESULT):])
            except ValueError:
                continue
            results[item.get("bvh")] = item
    tail = "\n".join((completed.stdout + completed.stderr).strip().splitlines()[-8:])
    return [results.get(job["bvh"]) or {**job, "ok": False,
                                        "error": f"Blender exited with code {completed.returncode}: {tail}"}
            for job in jobs]
