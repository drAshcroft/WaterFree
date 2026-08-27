"""Generate images and write them, with the settings that produced them.

Every image gets a sidecar `.json` carrying the prompt, seed, and full resolved
settings. Without it a promising image is unreproducible five minutes later,
which is the single most annoying failure mode of local generation.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from backend.imagegen import backend_diffusers as backend
from backend.imagegen.config import (
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
) -> dict:
    """Generate `count` images for `prompt` and return a JSON-shaped result."""
    text = prompt.strip()
    if not text:
        raise ValueError("Prompt is empty.")
    if count < 1 or count > MAX_BATCH:
        raise ValueError(f"count must be between 1 and {MAX_BATCH}.")

    spec = config.merged()
    backend.require_available()

    destination = Path(output_dir).expanduser() if output_dir else output_dir_for(config, workspace_path)
    destination.mkdir(parents=True, exist_ok=True)

    started = time.time()
    pipeline = backend.load_pipeline(spec, hf_token=hf_token)
    loaded = time.time()
    images = backend.run_pipeline(pipeline, spec, text, count=count)
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
            # a cold cache or a first-run download, a slow run is steps/size.
            "loadSeconds": round(loaded - started, 1),
            "generateSeconds": round(finished - loaded, 1),
        },
    }


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
    }


def _stem(prompt: str, timestamp: float) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:_SLUG_MAX_CHARS].strip("-")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(timestamp))
    return f"{stamp}_{slug}" if slug else stamp
