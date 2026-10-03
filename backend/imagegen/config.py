"""Model presets and workspace configuration for local image generation.

Everything here is configurable, in three layers, later winning over earlier:

    1. the preset's defaults          (this file)
    2. `.waterfree/imagegen.json`     (per workspace)
    3. CLI flags                      (per invocation)

Presets exist because "which model" is really six coupled decisions -- weights,
scheduler steps, guidance scale, resolution, dtype, and offload strategy -- and
getting one wrong on a 12 GB card means either an OOM or a washed-out image. A
preset is a known-good set; the layers above let you break it deliberately.

VRAM figures are measured-ish at the preset's own resolution with fp16/bf16
weights and no offloading. `fits_vram()` is advisory, not a gate: offloading
trades speed for headroom and can rescue a preset that "doesn't fit".
"""

from __future__ import annotations

import json
import os
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

CONFIG_PATH = (".waterfree", "imagegen.json")
DEFAULT_OUTPUT_DIR = (".waterfree", "generated")


@dataclass(frozen=True)
class ModelPreset:
    key: str
    repo_id: str
    label: str
    pipeline: str            # diffusers pipeline class name
    steps: int
    guidance: float
    width: int
    height: int
    dtype: str               # "bfloat16" | "float16"
    disk_gb: float
    vram_gb: float           # steady-state at this preset's resolution
    notes: str


PRESETS: dict[str, ModelPreset] = {
    "sd35-medium": ModelPreset(
        key="sd35-medium",
        repo_id="stabilityai/stable-diffusion-3.5-medium",
        label="Stable Diffusion 3.5 Medium",
        pipeline="StableDiffusion3Pipeline",
        steps=28,
        guidance=4.5,
        width=1024,
        height=1024,
        # SD3 was trained in bf16; float16 is a known source of black outputs here.
        dtype="bfloat16",
        disk_gb=11.0,
        vram_gb=9.5,
        notes="Best prompt adherence and legible text of the presets that fit 12 GB.",
    ),
    "sdxl": ModelPreset(
        key="sdxl",
        repo_id="stabilityai/stable-diffusion-xl-base-1.0",
        label="SDXL Base 1.0",
        pipeline="StableDiffusionXLPipeline",
        steps=30,
        guidance=7.0,
        width=1024,
        height=1024,
        dtype="float16",
        disk_gb=7.0,
        vram_gb=8.0,
        notes="Lighter and faster than SD3.5. Weaker at rendering text in-image.",
    ),
    "sdxl-turbo": ModelPreset(
        key="sdxl-turbo",
        repo_id="stabilityai/sdxl-turbo",
        label="SDXL Turbo",
        pipeline="AutoPipelineForText2Image",
        # Turbo is a distilled model: it takes 1-4 steps and NO guidance.
        # Passing a normal guidance scale here produces burnt, oversaturated output.
        steps=2,
        guidance=0.0,
        width=512,
        height=512,
        dtype="float16",
        disk_gb=7.0,
        vram_gb=6.5,
        notes="Sub-second drafts for iterating on a prompt. Not for finals.",
    ),
    "flux-schnell": ModelPreset(
        key="flux-schnell",
        repo_id="black-forest-labs/FLUX.1-schnell",
        label="FLUX.1 schnell",
        pipeline="FluxPipeline",
        steps=4,
        guidance=0.0,
        width=1024,
        height=1024,
        dtype="bfloat16",
        disk_gb=24.0,
        # Only fits 12 GB with sequential offload, which is slow. Kept as an
        # opt-in because its prompt adherence genuinely beats the others.
        vram_gb=22.0,
        notes="Highest quality, but needs sequential CPU offload on a 12 GB card.",
    ),
}

DEFAULT_PRESET = os.environ.get("WATERFREE_IMAGEGEN_PRESET", "sd35-medium").strip()

# Offload strategies, cheapest headroom first.
OFFLOAD_NONE = "none"
OFFLOAD_MODEL = "model"            # whole components moved GPU<->CPU between stages
OFFLOAD_SEQUENTIAL = "sequential"  # per-layer; large savings, large slowdown
OFFLOAD_MODES: tuple[str, ...] = (OFFLOAD_NONE, OFFLOAD_MODEL, OFFLOAD_SEQUENTIAL)


@dataclass(frozen=True)
class ImageGenConfig:
    preset: str = DEFAULT_PRESET
    repo_id: str = ""            # empty = take it from the preset
    steps: int = 0               # 0 = take it from the preset
    guidance: float = -1.0       # negative = take it from the preset
    width: int = 0
    height: int = 0
    dtype: str = ""
    negative_prompt: str = ""
    seed: int = -1               # negative = random
    offload: str = OFFLOAD_MODEL
    attention_slicing: bool = True
    vae_tiling: bool = True
    output_dir: str = ""
    backend: str = "diffusers"
    image: str = ""
    mask: str = ""
    strength: float = 0.75
    loras: tuple = ()             # {path: local file or repo ID, scale: float}
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_checkpoint: str = ""
    comfy_timeout: float = 300.0

    def resolved_preset(self) -> ModelPreset:
        key = self.preset.strip().lower()
        preset = PRESETS.get(key)
        if preset is None:
            known = ", ".join(sorted(PRESETS))
            raise ValueError(f"Unknown preset '{self.preset}'. Choose one of: {known}")
        return preset

    def merged(self) -> "ResolvedGeneration":
        """Collapse the preset and the overrides into concrete values."""
        preset = self.resolved_preset()
        if self.backend not in ("diffusers", "comfy"):
            raise ValueError("backend must be diffusers or comfy.")
        if self.mask and not self.image:
            raise ValueError("An inpainting mask requires an input image.")
        if not math.isfinite(float(self.strength)) or not 0 < float(self.strength) <= 1:
            raise ValueError("strength must be greater than 0 and at most 1.")
        if not math.isfinite(float(self.comfy_timeout)) or float(self.comfy_timeout) <= 0:
            raise ValueError("comfyTimeout must be positive and finite.")
        loras = normalize_loras(self.loras)
        if self.offload not in OFFLOAD_MODES:
            raise ValueError(f"Unknown offload strategy: {self.offload}")
        return ResolvedGeneration(
            preset=preset,
            repo_id=self.repo_id.strip() or preset.repo_id,
            steps=self.steps if self.steps > 0 else preset.steps,
            guidance=self.guidance if self.guidance >= 0 else preset.guidance,
            width=self.width if self.width > 0 else preset.width,
            height=self.height if self.height > 0 else preset.height,
            dtype=self.dtype.strip() or preset.dtype,
            negative_prompt=self.negative_prompt,
            seed=self.seed,
            offload=self.offload,
            attention_slicing=self.attention_slicing,
            vae_tiling=self.vae_tiling,
            backend=self.backend,
            image=str(Path(self.image).expanduser().resolve()) if self.image else '',
            mask=str(Path(self.mask).expanduser().resolve()) if self.mask else '',
            strength=float(self.strength), loras=loras,
            comfy_url=self.comfy_url, comfy_checkpoint=self.comfy_checkpoint,
            comfy_timeout=float(self.comfy_timeout),
        )


@dataclass(frozen=True)
class ResolvedGeneration:
    preset: ModelPreset
    repo_id: str
    steps: int
    guidance: float
    width: int
    height: int
    dtype: str
    negative_prompt: str
    seed: int
    offload: str
    attention_slicing: bool
    vae_tiling: bool
    backend: str = "diffusers"
    image: str = ""
    mask: str = ""
    strength: float = 0.75
    loras: tuple = ()
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_checkpoint: str = ""
    comfy_timeout: float = 300.0

    @property
    def mode(self) -> str:
        return "inpaint" if self.mask else "img2img" if self.image else "txt2img"

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "mode": self.mode,
            "preset": self.preset.key,
            "repoId": self.repo_id,
            "steps": self.steps,
            "guidance": self.guidance,
            "size": f"{self.width}x{self.height}",
            "dtype": self.dtype,
            "offload": self.offload,
            "image": self.image or None, "mask": self.mask or None,
            "strength": self.strength, "loras": list(self.loras),
            **({"comfyUrl": self.comfy_url, "checkpoint": self.comfy_checkpoint}
               if self.backend == "comfy" else {}),
        }


def normalize_loras(values) -> tuple:
    if not isinstance(values, (tuple, list)):
        raise ValueError("loras must be a list of paths or {path, scale} objects.")
    result = []
    for value in values:
        if isinstance(value, str):
            path, sep, weight = value.rpartition("=")
            value = {"path": path if sep else value, "scale": float(weight) if sep else 1.0}
        if not isinstance(value, dict) or not str(value.get("path", "")).strip():
            raise ValueError("Each LoRA requires a path.")
        weight = float(value.get("scale", 1.0))
        if not math.isfinite(weight):
            raise ValueError("LoRA scale must be finite.")
        path = str(value['path']).strip()
        local = Path(path).expanduser()
        if local.is_file():
            path = str(local.resolve())
        result.append({"path": path, "scale": weight})
    return tuple(result)


def load_config(workspace_path: str = "") -> ImageGenConfig:
    """Read `.waterfree/imagegen.json`, falling back to preset defaults.

    A malformed or unreadable config yields defaults rather than an error: a
    typo in an optional settings file should not make the command unusable.
    """
    if not workspace_path:
        return ImageGenConfig()
    path = Path(workspace_path).joinpath(*CONFIG_PATH)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ImageGenConfig()
    if not isinstance(raw, dict):
        return ImageGenConfig()

    known = {field for field in ImageGenConfig().__dataclass_fields__}
    payload = {
        _snake(key): value for key, value in raw.items()
        if _snake(key) in known and value is not None
    }
    if 'loras' in payload and isinstance(payload['loras'], list):
        payload['loras'] = tuple(payload['loras'])
    try:
        return ImageGenConfig(**payload)
    except TypeError:
        return ImageGenConfig()


def apply_overrides(config: ImageGenConfig, **overrides: Any) -> ImageGenConfig:
    """Layer CLI flags on top. `None` means "not supplied", not "clear it"."""
    supplied = {key: value for key, value in overrides.items() if value is not None}
    return replace(config, **supplied) if supplied else config


def write_default_config(workspace_path: str) -> Path:
    """Write a fully-populated config file so every knob is discoverable."""
    path = Path(workspace_path).joinpath(*CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {_camel(k): v for k, v in asdict(ImageGenConfig()).items()}
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


def output_dir_for(config: ImageGenConfig, workspace_path: str) -> Path:
    if config.output_dir.strip():
        return Path(config.output_dir).expanduser()
    root = Path(workspace_path) if workspace_path else Path.cwd()
    return root.joinpath(*DEFAULT_OUTPUT_DIR)


def available_vram_gb() -> float:
    """Total VRAM on the default CUDA device, or 0.0 when there is no GPU."""
    try:
        import torch  # noqa: PLC0415 -- heavy, and only needed for this check

        if not torch.cuda.is_available():
            return 0.0
        return torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    except Exception:
        return 0.0


def fits_vram(preset: ModelPreset, vram_gb: float = -1.0) -> bool:
    total = available_vram_gb() if vram_gb < 0 else vram_gb
    if total <= 0:
        return False
    return preset.vram_gb <= total


def _snake(key: str) -> str:
    out = []
    for char in str(key):
        if char.isupper():
            out.append("_")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out)


def _camel(key: str) -> str:
    head, *rest = str(key).split("_")
    return head + "".join(part.title() for part in rest)
