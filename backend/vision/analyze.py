"""Run a local vision model over one or more images.

Images are downscaled before they are sent. This is not cosmetic: a 4K
screenshot costs a vision model thousands of image tokens and most of a 12 GB
card's headroom, for detail no model actually uses. 1280px on the long edge
keeps UI text legible while keeping a page review to a few seconds.
"""

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from pathlib import Path

from backend.llm import ollama_client
from backend.vision.models import (
    DEFAULT_PURPOSE,
    Purpose,
    ensure_available,
    get_purpose,
    resolve_model,
)

# Ollama sniffs the format from the bytes, so no media type is needed -- but the
# extension is still the cheapest way to reject a PDF before decoding it.
SUPPORTED_SUFFIXES: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
})

MAX_EDGE_PX = int(os.environ.get("WATERFREE_VISION_MAX_EDGE", "1280"))
# Guards against handing a multi-hundred-megabyte file to PIL.
MAX_FILE_BYTES = int(os.environ.get("WATERFREE_VISION_MAX_BYTES", str(64 * 1024 * 1024)))
MAX_IMAGES = int(os.environ.get("WATERFREE_VISION_MAX_IMAGES", "4"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("WATERFREE_VISION_TIMEOUT", "300"))
# Vision models on a 12 GB card are slow to load; keeping one resident between
# calls is the difference between a 4-second and a 40-second second image.
KEEP_ALIVE = os.environ.get("WATERFREE_VISION_KEEP_ALIVE", "15m")


class VisionError(RuntimeError):
    """The image could not be read, or the model could not be reached."""


@dataclass(frozen=True)
class PreparedImage:
    path: str
    b64: str
    width: int
    height: int
    original_width: int
    original_height: int

    @property
    def was_downscaled(self) -> bool:
        return (self.width, self.height) != (self.original_width, self.original_height)


def analyze(
    image_paths: list[str],
    *,
    purpose: str = DEFAULT_PURPOSE,
    question: str = "",
    model: str = "",
    tier: str = "",
    base: str = "",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Analyze images and return a JSON-shaped result.

    Multiple images go into a single turn, which is what makes `compare` work:
    the model sees them together rather than one at a time with no memory.
    """
    if not image_paths:
        raise ValueError("No image supplied.")
    if len(image_paths) > MAX_IMAGES:
        raise ValueError(
            f"{len(image_paths)} images supplied; the limit is {MAX_IMAGES}. "
            "More than that exceeds the context of a local vision model and "
            "produces a summary of nothing in particular."
        )

    resolved_purpose = get_purpose(purpose)
    model_id = resolve_model(resolved_purpose, override=model, tier=tier)
    ensure_available(model_id, base=base)

    prepared = [prepare_image(path) for path in image_paths]
    prompt = _build_prompt(resolved_purpose, question, prepared)

    kwargs = {"base": base} if base else {}
    try:
        answer = ollama_client.chat(
            model=model_id,
            messages=[
                {"role": "system", "content": resolved_purpose.system},
                {"role": "user", "content": prompt},
            ],
            images=[item.b64 for item in prepared],
            timeout=timeout,
            keep_alive=KEEP_ALIVE,
            **kwargs,
        )
    except ollama_client.OllamaError as exc:
        raise VisionError(str(exc)) from exc

    return {
        "purpose": resolved_purpose.key,
        "model": model_id,
        "question": question.strip() or resolved_purpose.default_question,
        "answer": answer.strip(),
        "images": [
            {
                "path": item.path,
                "sent": f"{item.width}x{item.height}",
                "original": f"{item.original_width}x{item.original_height}",
                "downscaled": item.was_downscaled,
            }
            for item in prepared
        ],
    }


def _build_prompt(purpose: Purpose, question: str, prepared: list[PreparedImage]) -> str:
    ask = question.strip() or purpose.default_question
    if len(prepared) == 1:
        return ask
    # Numbering the images is what lets the answer refer to them unambiguously;
    # without it a multi-image reply says "the first one" and means anyone's guess.
    listing = "\n".join(
        f"[image {i}] {Path(item.path).name}" for i, item in enumerate(prepared, start=1)
    )
    return f"{ask}\n\nThe images, in order:\n{listing}"


def prepare_image(path: str) -> PreparedImage:
    """Validate, downscale, and base64-encode one image."""
    resolved = Path(path).expanduser()
    if not resolved.exists():
        raise VisionError(f"Image not found: {resolved}")
    if not resolved.is_file():
        raise VisionError(f"Not a file: {resolved}")
    if resolved.suffix.lower() not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise VisionError(
            f"Unsupported image type '{resolved.suffix}'. Supported: {supported}"
        )
    size = resolved.stat().st_size
    if size > MAX_FILE_BYTES:
        raise VisionError(
            f"{resolved.name} is {size / 1e6:.0f} MB, over the "
            f"{MAX_FILE_BYTES / 1e6:.0f} MB limit."
        )

    try:
        from PIL import Image  # noqa: PLC0415 -- optional at import time, required here
    except ImportError as exc:  # pragma: no cover - Pillow ships with the venv
        raise VisionError(
            "Pillow is required for vision. Install it with: pip install pillow"
        ) from exc

    try:
        with Image.open(resolved) as image:
            image.load()
            original_width, original_height = image.size
            converted = image.convert("RGB")
    except Exception as exc:
        raise VisionError(f"Could not read {resolved.name}: {exc}") from exc

    longest = max(original_width, original_height)
    if longest > MAX_EDGE_PX:
        scale = MAX_EDGE_PX / float(longest)
        target = (max(1, int(original_width * scale)), max(1, int(original_height * scale)))
        from PIL import Image as _Image  # noqa: PLC0415

        converted = converted.resize(target, _Image.LANCZOS)

    buffer = io.BytesIO()
    # PNG rather than JPEG: screenshots are the primary input here and JPEG
    # artefacts around small UI text are exactly what breaks transcription.
    converted.save(buffer, format="PNG", optimize=True)
    width, height = converted.size

    return PreparedImage(
        path=str(resolved),
        b64=base64.b64encode(buffer.getvalue()).decode("ascii"),
        width=width,
        height=height,
        original_width=original_width,
        original_height=original_height,
    )
