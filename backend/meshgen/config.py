"""Settings for image-to-3D generation with trellis.cpp.

Same three layers as imagegen, later winning:

    1. the defaults below
    2. ``.waterfree/meshgen.json``   (per workspace; `waterfree meshgen init` writes it)
    3. CLI flags                      (per invocation)

Game assets want far fewer triangles than trellis's fixed 150K/300K targets,
but trellis.cpp v0.8.1 offers no way to ask for fewer (see ``faces``); a
WaterFree-side decimation pass is the planned fix (GPU-009).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

CONFIG_PATH = (".waterfree", "meshgen.json")
DEFAULT_OUTPUT_DIR = (".waterfree", "generated", "meshes")

RESOLUTIONS = (512, 1024, 1536)
BG_REMOVAL_MODES = ("auto", "birefnet", "threshold")
UV_MODES = ("xatlas", "box")


@dataclass(frozen=True)
class MeshgenConfig:
    model_set: str = "trellis2-q8"
    resolution: int = 1024
    # Triangle budget. trellis.cpp v0.8.1 cannot honour one: its CLI rejects
    # --faces and its server ignores the "faces" field (both are on trellis.cpp
    # main, not in a release), so meshes come out at its fixed QEM targets,
    # ~150K triangles at res 512 and ~300K at 1024. 0 = that. Anything else is
    # refused until WaterFree has its own decimation pass (GPU-009).
    faces: int = 0
    atlas: int = 0              # UV atlas px; 0 = engine default (2048 @1024, 1024 @512)
    texture: bool = True
    bg_removal: str = "auto"
    uv: str = "xatlas"
    steps: int = 12
    seed: int = -1              # negative = a fresh random seed per image (recorded in the sidecar)
    copyright: str = ""
    output_dir: str = ""

    def validate(self) -> MeshgenConfig:
        if self.resolution not in RESOLUTIONS:
            raise ValueError(f"resolution must be one of {RESOLUTIONS}")
        if self.faces:
            raise ValueError(
                "--faces is not available yet: trellis.cpp v0.8.1 has a fixed triangle target "
                "(~150K at --res 512, ~300K at --res 1024) that neither its CLI nor its server lets a "
                "caller change. Decimate the GLB afterwards (e.g. Blender's Decimate modifier) until "
                "WaterFree's own decimation pass lands (todo GPU-009).")
        if self.atlas and self.atlas not in (512, 1024, 2048, 4096):
            raise ValueError("atlas must be 0 (engine default), 512, 1024, 2048 or 4096")
        if self.bg_removal not in BG_REMOVAL_MODES:
            raise ValueError(f"bg_removal must be one of {BG_REMOVAL_MODES}")
        if self.uv not in UV_MODES:
            raise ValueError(f"uv must be one of {UV_MODES}")
        if not 1 <= self.steps <= 100:
            raise ValueError("steps must be between 1 and 100")
        return self

    def describe(self) -> dict:
        return {
            "modelSet": self.model_set,
            "resolution": self.resolution,
            "faces": self.faces or "engine fixed (~150K @512, ~300K @1024)",
            "atlas": self.atlas or "engine default",
            "texture": self.texture,
            "bgRemoval": self.bg_removal,
            "uv": self.uv,
            "steps": self.steps,
        }


def load_config(workspace_path: str = "") -> MeshgenConfig:
    """Read ``.waterfree/meshgen.json``; a malformed file falls back to defaults."""
    if not workspace_path:
        return MeshgenConfig()
    path = Path(workspace_path).joinpath(*CONFIG_PATH)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MeshgenConfig()
    if not isinstance(raw, dict):
        return MeshgenConfig()
    known = set(MeshgenConfig.__dataclass_fields__)
    payload = {_snake(k): v for k, v in raw.items() if _snake(k) in known and v is not None}
    try:
        return MeshgenConfig(**payload)
    except TypeError:
        return MeshgenConfig()


def apply_overrides(config: MeshgenConfig, **overrides: Any) -> MeshgenConfig:
    supplied = {k: v for k, v in overrides.items() if v is not None}
    return replace(config, **supplied) if supplied else config


def write_default_config(workspace_path: str) -> Path:
    path = Path(workspace_path).joinpath(*CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {_camel(k): v for k, v in asdict(MeshgenConfig()).items()}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


def output_dir_for(config: MeshgenConfig, workspace_path: str) -> Path:
    if config.output_dir.strip():
        return Path(config.output_dir).expanduser()
    return Path(workspace_path or ".").joinpath(*DEFAULT_OUTPUT_DIR)


def _snake(key: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in str(key))


def _camel(key: str) -> str:
    head, *rest = str(key).split("_")
    return head + "".join(part.title() for part in rest)
