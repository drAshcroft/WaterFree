"""diffusers pipeline construction and execution.

Kept behind a thin seam (`load_pipeline` / `run_pipeline`) so a ComfyUI or
remote backend can slot in later without the CLI or the skill changing.

Every diffusers import is deferred to call time. The package pulls in torch and
takes seconds to import, and `waterfree imagegen models` -- the command you run
to find out whether any of this is installed -- must work without it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from backend.imagegen.config import (
    OFFLOAD_MODEL,
    OFFLOAD_SEQUENTIAL,
    ResolvedGeneration,
)

log = logging.getLogger(__name__)

# One process, one loaded pipeline. Loading SD3.5-Medium takes ~30 s and ~10 GB;
# regenerating with a tweaked prompt must not pay that twice.
_PIPELINE_CACHE: dict[str, Any] = {}


class BackendUnavailable(RuntimeError):
    """diffusers/torch are missing, or there is no usable GPU."""


class GenerationFailed(RuntimeError):
    """The pipeline loaded but the run failed."""


def is_available() -> tuple[bool, str]:
    """(usable, reason). Reason is empty when usable."""
    try:
        import torch  # noqa: PLC0415
    except ImportError:
        return False, "PyTorch is not installed."
    try:
        import diffusers  # noqa: PLC0415,F401
    except ImportError:
        return False, (
            "diffusers is not installed. Install it with:\n"
            "  pip install diffusers accelerate sentencepiece protobuf"
        )
    if not torch.cuda.is_available():
        return False, (
            "No CUDA device is visible to PyTorch. Generation on CPU is not "
            "supported here -- it takes minutes per image."
        )
    return True, ""


def require_available() -> None:
    usable, reason = is_available()
    if not usable:
        raise BackendUnavailable(reason)


def load_pipeline(spec: ResolvedGeneration, *, hf_token: str = "") -> Any:
    """Return a ready pipeline for `spec`, from cache when possible."""
    require_available()
    cache_key = f"{spec.repo_id}|{spec.dtype}|{spec.offload}"
    cached = _PIPELINE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    import torch  # noqa: PLC0415

    dtype = getattr(torch, spec.dtype, None)
    if dtype is None:
        raise GenerationFailed(f"Unknown dtype '{spec.dtype}'.")

    pipeline_class = _pipeline_class(spec.preset.pipeline)
    token = hf_token.strip() or os.environ.get("HF_TOKEN", "").strip()
    kwargs: dict[str, Any] = {"torch_dtype": dtype}
    if token:
        # SD3.5 and FLUX are gated repos: without an accepted licence and a
        # token, the download 401s with a message that does not mention either.
        kwargs["token"] = token

    try:
        pipeline = pipeline_class.from_pretrained(spec.repo_id, **kwargs)
    except Exception as exc:
        raise GenerationFailed(_download_hint(spec, exc)) from exc

    _apply_memory_strategy(pipeline, spec)
    _PIPELINE_CACHE[cache_key] = pipeline
    return pipeline


def run_pipeline(pipeline: Any, spec: ResolvedGeneration, prompt: str, *, count: int) -> list[Any]:
    """Generate `count` images and return them as PIL images."""
    import torch  # noqa: PLC0415

    generator = None
    if spec.seed >= 0:
        # Seeded on the CPU generator: CUDA generators are not reproducible
        # across driver versions, which defeats the point of pinning a seed.
        generator = torch.Generator(device="cpu").manual_seed(spec.seed)

    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "num_inference_steps": spec.steps,
        "guidance_scale": spec.guidance,
        "width": spec.width,
        "height": spec.height,
        "num_images_per_prompt": count,
    }
    if generator is not None:
        kwargs["generator"] = generator
    if spec.negative_prompt.strip() and _supports_negative_prompt(spec):
        kwargs["negative_prompt"] = spec.negative_prompt.strip()

    try:
        result = pipeline(**kwargs)
    except torch.cuda.OutOfMemoryError as exc:
        raise GenerationFailed(_oom_hint(spec)) from exc
    except Exception as exc:
        raise GenerationFailed(f"Generation failed: {exc}") from exc

    return list(result.images)


def unload() -> None:
    """Drop cached pipelines and release VRAM."""
    _PIPELINE_CACHE.clear()
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _pipeline_class(name: str) -> Any:
    import diffusers  # noqa: PLC0415

    cls = getattr(diffusers, name, None)
    if cls is None:
        raise GenerationFailed(
            f"This diffusers version has no '{name}'. Upgrade it with:\n"
            "  pip install -U diffusers"
        )
    return cls


def _apply_memory_strategy(pipeline: Any, spec: ResolvedGeneration) -> None:
    """Fit the pipeline into available VRAM.

    Order matters: offloading must be configured instead of `.to("cuda")`, not
    alongside it -- moving the whole pipeline onto the GPU first defeats the
    offload hooks and reintroduces the OOM they exist to prevent.
    """
    if spec.offload == OFFLOAD_SEQUENTIAL:
        _try(pipeline, "enable_sequential_cpu_offload")
    elif spec.offload == OFFLOAD_MODEL:
        _try(pipeline, "enable_model_cpu_offload")
    else:
        try:
            pipeline.to("cuda")
        except Exception as exc:
            raise GenerationFailed(f"Could not move the pipeline to the GPU: {exc}") from exc

    if spec.attention_slicing:
        _try(pipeline, "enable_attention_slicing")
    if spec.vae_tiling:
        # Tiled VAE decode is what keeps the final decode step from spiking
        # past the headroom the rest of the pipeline just saved.
        _try(pipeline, "enable_vae_tiling")


def _try(pipeline: Any, method_name: str) -> None:
    """Call an optional pipeline optimisation, ignoring absence."""
    method = getattr(pipeline, method_name, None)
    if method is None:
        log.debug("Pipeline has no %s(); skipping.", method_name)
        return
    try:
        method()
    except Exception as exc:
        log.warning("%s() failed, continuing without it: %s", method_name, exc)


def _supports_negative_prompt(spec: ResolvedGeneration) -> bool:
    """Distilled models ignore or reject a negative prompt.

    FLUX.schnell and SDXL-Turbo run without classifier-free guidance, so there
    is no unconditional branch for a negative prompt to steer.
    """
    return spec.guidance > 0.0


def _download_hint(spec: ResolvedGeneration, exc: Exception) -> str:
    detail = str(exc)
    base = f"Could not load '{spec.repo_id}': {detail}"
    lowered = detail.lower()
    if "401" in detail or "gated" in lowered or "authorized" in lowered:
        return (
            f"{base}\n\n'{spec.repo_id}' is a gated repository. Accept its licence "
            f"at https://huggingface.co/{spec.repo_id} then set $HF_TOKEN "
            "(https://huggingface.co/settings/tokens)."
        )
    if "connection" in lowered or "resolve" in lowered or "timed out" in lowered:
        return (
            f"{base}\n\nThe weights are downloaded on first use "
            f"(~{spec.preset.disk_gb:.0f} GB for this preset) and need network access."
        )
    return base


def _oom_hint(spec: ResolvedGeneration) -> str:
    steps = [
        "  --offload sequential      (largest saving, slowest)",
        "  --width 768 --height 768  (VRAM scales with area)",
    ]
    if spec.preset.key != "sdxl-turbo":
        steps.append("  --preset sdxl-turbo       (lightest preset)")
    return (
        f"Ran out of VRAM generating at {spec.width}x{spec.height} with "
        f"'{spec.preset.key}' (offload={spec.offload}). Try, in order:\n"
        + "\n".join(steps)
        + "\n\nClose other GPU users first -- a resident Ollama model holds "
        "several GB. Free it with: ollama stop <model>"
    )
