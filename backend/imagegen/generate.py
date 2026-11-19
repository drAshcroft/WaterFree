"""Generate images and write them, with the settings that produced them.

Every image gets a sidecar `.json` carrying the prompt, seed, and full resolved
settings. Without it a promising image is unreproducible five minutes later,
which is the single most annoying failure mode of local generation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

from backend.gpu import lease as gpu_lease
from backend.gpu.preflight import exclusive_gpu
from backend.gpu.spill import SpillWatch
from backend.imagegen import backend_diffusers as backend
from backend.imagegen.config import (
    OFFLOAD_SEQUENTIAL,
    ImageGenConfig,
    ResolvedGeneration,
    output_dir_for,
)

MAX_BATCH = 8
_SLUG_MAX_CHARS = 40


def generate(
    prompt: str,
    *,
    config: ImageGenConfig,
    workspace_path: str = "",
    count: int = 1,
    output_dir: str = "",
    hf_token: str = "",
    policy: gpu_lease.WaitPolicy | None = None,
    check_vram: bool = True,
) -> dict:
    """Generate `count` images for `prompt` and return a JSON-shaped result."""
    text = prompt.strip()
    if not text:
        raise ValueError("Prompt is empty.")
    if count < 1 or count > MAX_BATCH:
        raise ValueError(f"count must be between 1 and {MAX_BATCH}.")

    spec = config.merged()
    # Validate input data before allocating a model or requesting any downloads.
    from PIL import Image
    for name in ('image', 'mask'):
        path = getattr(spec, name)
        if path:
            try:
                with Image.open(path) as source:
                    source.verify()
            except (OSError, ValueError) as exc:
                raise ValueError(f"Invalid {name} file '{path}': {exc}") from exc
    if spec.backend == 'diffusers':
        import re as _re
        for lora in spec.loras:
            path = lora['path']
            missing_local = Path(path).suffix.lower() in ('.safetensors', '.bin', '.pt', '.pth')
            if not Path(path).is_file() and (missing_local or not _re.fullmatch(r'[\w.-]+/[\w.-]+', path)):
                raise ValueError(f"LoRA file does not exist: {path}")
    selected_backend = backend
    if spec.backend == 'comfy':
        from backend.imagegen import backend_comfy
        selected_backend = backend_comfy
        selected_backend.require_available(spec)
    else:
        selected_backend.require_available()

    destination = Path(output_dir).expanduser() if output_dir else output_dir_for(config, workspace_path)
    destination.mkdir(parents=True, exist_ok=True)

    with _gpu_for(spec, policy=policy, check_vram=check_vram, count=count) as grant, \
            _spill_watch(spec) as spill:
        started = time.time()  # after the queue: timing measures the work, not the wait
        pipeline = selected_backend.load_pipeline(spec, hf_token=hf_token)
        if spec.backend == 'comfy':
            spec = replace(spec, comfy_checkpoint=pipeline['checkpoint'])
        loaded = time.time()
        images = selected_backend.run_pipeline(pipeline, spec, text, count=count)
        comfy_freed = None
        if spec.backend == 'diffusers':
            # Hand the card back clean: the next job in the queue checks free
            # VRAM the moment this lease is released, before this process exits.
            del pipeline
            selected_backend.unload()
        elif spec.comfy_free_after and _is_local(spec.comfy_url):
            comfy_freed = selected_backend.free(spec)
    if len(images) != count:
        raise backend.GenerationFailed(f"Backend returned {len(images)} images; expected {count}.")
    if spec.mask:
        from PIL import ImageOps
        with Image.open(spec.image) as source:
            original = ImageOps.exif_transpose(source).convert('RGB').resize((spec.width, spec.height))
        with Image.open(spec.mask) as source:
            mask = source.convert('L').resize(original.size, Image.Resampling.NEAREST)
        images = [Image.composite(value.convert('RGB').resize(original.size), original, mask) for value in images]
    finished = time.time()

    stem = _stem(text, started)
    written: list[dict] = []
    for index, image in enumerate(images):
        suffix = "" if len(images) == 1 else f"_{index + 1}"
        image_path = destination / f"{stem}{suffix}.png"
        image.save(image_path)
        sidecar = image_path.with_suffix(".json")
        metadata = _metadata(text, spec, seed_offset=index)
        sidecar.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        written.append({"image": str(image_path), "metadata": str(sidecar)})

    return {
        "prompt": text,
        "count": len(written),
        "outputDir": str(destination),
        "files": written,
        "settings": spec.describe(),
        "timing": {
            # Split out because they have very different causes: a slow load is
            # a cold disk cache, a slow run is steps/size (or VRAM spilling).
            "loadSeconds": round(loaded - started, 1),
            "generateSeconds": round(finished - loaded, 1),
        },
        **({"gpu": grant.public()} if grant is not None else {}),
        **({"vramSpill": spill.report_json()} if spill is not None else {}),
        **({"comfyFreed": comfy_freed} if comfy_freed is not None else {}),
    }


@contextmanager
def _spill_watch(spec: ResolvedGeneration) -> Iterator:
    """Watch this process for a VRAM spill while diffusers runs in it (todo GPU-005).

    ComfyUI is a separate program managing its own memory, so it is not watched.
    """
    if spec.backend != "diffusers":
        yield None
        return
    with SpillWatch(os.getpid(), label=f"imagegen ({spec.preset.key})") as watch:
        yield watch


# Free VRAM each (preset, offload) needs: the nvidia-smi peak above the desktop's
# own use on an RTX 3060 12 GB, one image at the preset's default size, measured
# 2026-11-18 (todo GPU-008), plus ~0.3 GB headroom where the card has it.
# "none" does not fit a 12 GB card for SDXL or SD3.5, so its figure is set past
# the card and the preflight refuses it instead of crashing or crawling.
MEASURED_VRAM_GB: dict[tuple[str, str], float] = {
    ("sdxl-turbo", "none"): 9.2,         # peak 8.92
    ("sdxl-turbo", "model"): 5.6,        # peak 5.32
    ("sdxl-turbo", "sequential"): 1.0,   # peak 0.67
    ("sdxl", "none"): 12.8,              # peak 9.62 on the card + 3.15 GB spilled into system RAM
    ("sdxl", "model"): 7.9,              # peak 7.64
    ("sdxl", "sequential"): 3.4,         # peak 3.07; generation 3.4x slower than model offload
    ("sd35-medium", "none"): 13.0,       # crashed (0xC0000005) after 83 s loading fully onto the card
    ("sd35-medium", "model"): 10.3,      # peak 10.26: the whole card next to the desktop, no headroom left
    ("sd35-medium", "sequential"): 3.3,  # peak 3.00; generation 1.7x slower than model offload
}


def vram_need_gb(spec: ResolvedGeneration) -> float | None:
    """Free VRAM to require before loading: measured per (preset, offload), else an estimate.

    Unmeasured combinations (flux-schnell; other sizes count as the preset's
    default) fall back to the preset's figure, or 3 GB for sequential offload,
    which streams layers and needs little.
    """
    measured = MEASURED_VRAM_GB.get((spec.preset.key, spec.offload))
    if measured is not None:
        return measured
    if spec.offload == OFFLOAD_SEQUENTIAL:
        return 3.0
    return spec.preset.vram_gb


@contextmanager
def _gpu_for(spec: ResolvedGeneration, *, policy, check_vram: bool, count: int) -> Iterator:
    """The exclusive GPU lease for a diffusers run, or for a ComfyUI server on this machine."""
    detail = f"{spec.preset.key}, {spec.width}x{spec.height}, {count} image(s)"
    if spec.backend == "comfy":
        if not _is_local(spec.comfy_url):
            yield None
            return
        # A local ComfyUI uses this GPU but manages its own memory: hold the
        # lease so nothing else starts meanwhile, but skip the VRAM check.
        with exclusive_gpu(label="imagegen make", detail=f"comfy {detail}", policy=policy,
                           check_vram=False) as grant:
            yield grant
        return
    with exclusive_gpu(label="imagegen make", detail=detail, vram_gb=vram_need_gb(spec),
                       policy=policy, check_vram=check_vram) as grant:
        yield grant


def _is_local(url: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


def _metadata(prompt: str, spec: ResolvedGeneration, *, seed_offset: int) -> dict:
    preset = asdict(spec.preset)
    return {
        "prompt": prompt,
        "negativePrompt": spec.negative_prompt,
        "repoId": spec.repo_id,
        "preset": preset,
        "steps": spec.steps,
        "guidance": spec.guidance,
        "width": spec.width,
        "height": spec.height,
        "dtype": spec.dtype,
        "offload": spec.offload,
        # A batch shares one generator, so only image 1 is reproducible from the
        # recorded seed. Recording it as null for the rest is honest; recording
        # the base seed on all of them would be a lie that wastes someone's time.
        "seed": spec.seed if (spec.seed >= 0 and seed_offset == 0) else None,
        "batchIndex": seed_offset,
        "backend": spec.backend, "mode": spec.mode, "strength": spec.strength,
        "inputs": {name: {"path": getattr(spec, name),
                          "sha256": _file_hash(getattr(spec, name))}
                   for name in ('image', 'mask') if getattr(spec, name)},
        "loras": [{**lora, "sha256": _file_hash(lora['path'])} for lora in spec.loras],
        **({"comfyUrl": spec.comfy_url, "checkpoint": spec.comfy_checkpoint,
            "comfyCfg": spec.guidance if spec.guidance > 0 else 1.0} if spec.backend == 'comfy' else {}),
    }


def _file_hash(path):
    if not Path(path).is_file():
        return None
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _stem(prompt: str, timestamp: float) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:_SLUG_MAX_CHARS].strip("-")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(timestamp))
    # Concurrent requests in the same second must not overwrite earlier output.
    return f"{stamp}_{slug}_{uuid.uuid4().hex[:8]}" if slug else f"{stamp}_{uuid.uuid4().hex[:8]}"
