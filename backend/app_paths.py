"""Machine-wide WaterFree directories under %LOCALAPPDATA%\\WaterFree.

Per-workspace state lives in ``<workspace>/.waterfree``; the knowledge base
lives in ``~/.waterfree/global``. This module covers the third kind: state that
belongs to the *machine* rather than to a project or a user's notes -- the GPU
lease, installed inference engines, and their multi-gigabyte model weights.

Every root can be redirected by an environment variable, which is also how the
tests keep their state out of the real directories.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def local_app_dir() -> Path:
    """``%LOCALAPPDATA%\\WaterFree`` on Windows, ``~/.local/share/waterfree`` elsewhere."""
    override = os.environ.get("WATERFREE_LOCAL_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if base:
            return Path(base) / "WaterFree"
    return Path.home() / ".local" / "share" / "waterfree"


def gpu_state_dir() -> Path:
    """Lease and queue state shared by every WaterFree process on this machine."""
    override = os.environ.get("WATERFREE_GPU_DIR", "").strip()
    return Path(override).expanduser() if override else local_app_dir() / "gpu"


def engines_dir() -> Path:
    """Installed native inference engines (trellis.cpp, openmoss), one folder per version."""
    override = os.environ.get("WATERFREE_ENGINES_DIR", "").strip()
    return Path(override).expanduser() if override else local_app_dir() / "engines"


def models_dir() -> Path:
    """Model weights for those engines. Imagegen keeps using the Hugging Face cache."""
    override = os.environ.get("WATERFREE_MODELS_DIR", "").strip()
    return Path(override).expanduser() if override else local_app_dir() / "models"


def engine_logs_dir() -> Path:
    return local_app_dir() / "logs" / "engines"
