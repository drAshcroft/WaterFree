"""Settings for text-to-motion with Kimodo.

Three layers, later winning: the defaults below, ``.waterfree/animgen.json``
(`waterfree animgen init` writes it), then CLI flags.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

CONFIG_PATH = (".waterfree", "animgen.json")
DEFAULT_OUTPUT_DIR = (".waterfree", "generated", "animations")

TPOSES = ("standard", "seed")
FBX_MODES = ("auto", "always", "never")
# Kimodo is trained on clips up to 10 s; a multi-segment sequence chains
# segments with short transitions, so only each segment is held to that.
MAX_SEGMENT_SECONDS = 10.0
MAX_SEQUENCE_SECONDS = 60.0
MAX_VARIANTS = 8
# A loop is cut from a longer generation (cycle * 1.4 + 1.5 s, capped at 10 s),
# so the cycle itself is held to 6 s.
MAX_LOOP_SECONDS = 6.0


@dataclass(frozen=True)
class AnimgenConfig:
    seconds: float = 4.0            # clip length when a prompt gives none
    steps: int = 100                # diffusion steps (Kimodo's default)
    seed: int = -1                  # negative = a fresh random seed per clip (recorded in the sidecar)
    in_place: bool = False          # strip the travel (smoothed root x/z) for engine-driven locomotion
    # Make each clip a seamless cycle of about `seconds`: generate longer, cut
    # at the best-matching frame pair, crossfade the seam (ANIM-003).
    loop: bool = False
    preview: bool = True            # write a stick-figure contact sheet PNG per clip (ANIM-005)
    postprocess: bool = True        # Kimodo's foot-skate / constraint clean-up
    transition_frames: int = 5      # blend frames between segments of a sequence
    # BVH rest pose: "standard" = a T-pose, which retargeting tools expect;
    # "seed" = the BONES-SEED dataset's own rest pose.
    tpose: str = "standard"
    # FBX through Blender (Unity cannot import BVH): "auto" converts when
    # Blender is found, "always" fails up front without it, "never" skips it.
    fbx: str = "auto"
    blender_path: str = ""          # empty = find it (PATH, then Program Files\Blender Foundation)
    # Free system RAM required before the CPU text encoder (Llama-3-8B, bf16)
    # loads: measured peak 15.5 GB on 2026-11-18, plus headroom.
    ram_gb: float = 17.0
    output_dir: str = ""

    def validate(self) -> AnimgenConfig:
        if not 0.5 <= float(self.seconds) <= MAX_SEGMENT_SECONDS:
            raise ValueError(f"seconds must be between 0.5 and {MAX_SEGMENT_SECONDS:g}")
        if self.loop and float(self.seconds) > MAX_LOOP_SECONDS:
            raise ValueError(f"a loop is at most {MAX_LOOP_SECONDS:g} s (it is cut from a longer generation)")
        if not 10 <= int(self.steps) <= 1000:
            raise ValueError("steps must be between 10 and 1000")
        if not 0 <= int(self.transition_frames) <= 30:
            raise ValueError("transition_frames must be between 0 and 30")
        if self.tpose not in TPOSES:
            raise ValueError(f"tpose must be one of {TPOSES}")
        if self.fbx not in FBX_MODES:
            raise ValueError(f"fbx must be one of {FBX_MODES}")
        if not 1.0 <= float(self.ram_gb) <= 512.0:
            raise ValueError("ram_gb must be between 1 and 512")
        return self

    def describe(self) -> dict:
        return {
            "steps": self.steps,
            "inPlace": self.in_place,
            "loop": self.loop,
            "postprocess": self.postprocess,
            "transitionFrames": self.transition_frames,
            "bvhRestPose": "T-pose" if self.tpose == "standard" else "BONES-SEED rest pose",
        }


def load_config(workspace_path: str = "") -> AnimgenConfig:
    """Read ``.waterfree/animgen.json``; a malformed file falls back to defaults."""
    if not workspace_path:
        return AnimgenConfig()
    path = Path(workspace_path).joinpath(*CONFIG_PATH)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AnimgenConfig()
    if not isinstance(raw, dict):
        return AnimgenConfig()
    known = set(AnimgenConfig.__dataclass_fields__)
    payload = {_snake(k): v for k, v in raw.items() if _snake(k) in known and v is not None}
    try:
        return AnimgenConfig(**payload)
    except TypeError:
        return AnimgenConfig()


def apply_overrides(config: AnimgenConfig, **overrides: Any) -> AnimgenConfig:
    supplied = {k: v for k, v in overrides.items() if v is not None}
    return replace(config, **supplied) if supplied else config


def write_default_config(workspace_path: str) -> Path:
    path = Path(workspace_path).joinpath(*CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {_camel(k): v for k, v in asdict(AnimgenConfig()).items()}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


def output_dir_for(config: AnimgenConfig, workspace_path: str) -> Path:
    if config.output_dir.strip():
        return Path(config.output_dir).expanduser()
    return Path(workspace_path or ".").joinpath(*DEFAULT_OUTPUT_DIR)


def _snake(key: str) -> str:
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in str(key))


def _camel(key: str) -> str:
    head, *rest = str(key).split("_")
    return head + "".join(part.title() for part in rest)
