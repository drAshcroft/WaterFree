"""Run a local vision model over one or more images.

Images are downscaled before they are sent. This is not cosmetic: a 4K
screenshot costs a vision model thousands of image tokens and most of a 12 GB
card's headroom, for detail no model actually uses. 1280px on the long edge
keeps UI text legible while keeping a page review to a few seconds.
"""

from __future__ import annotations

import base64
import functools
import io
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

from backend.llm import ollama_client
from backend.vision import classify
from backend.vision.models import (
    DEFAULT_PURPOSE,
    LIST_TIMEOUT_SECONDS,
    UNVERIFIED,
    Purpose,
    ensure_available,
    get_purpose,
    missing_model_error,
    resolve_route,
)
from backend.vision.questions import leading_question_warning

# Ollama sniffs the format from the bytes, so no media type is needed -- but the
# extension is still the cheapest way to reject a PDF before decoding it.
SUPPORTED_SUFFIXES: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
})

MAX_EDGE_PX = int(os.environ.get("WATERFREE_VISION_MAX_EDGE", "1280"))
# Guards against handing a multi-hundred-megabyte file to PIL.
MAX_FILE_BYTES = int(os.environ.get("WATERFREE_VISION_MAX_BYTES", str(64 * 1024 * 1024)))
MAX_IMAGES = int(os.environ.get("WATERFREE_VISION_MAX_IMAGES", "4"))
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("WATERFREE_VISION_TIMEOUT", "300"))
# Vision models on a 12 GB card are slow to load; keeping one resident between
# calls is the difference between a 4-second and a 40-second second image.
KEEP_ALIVE = os.environ.get("WATERFREE_VISION_KEEP_ALIVE", "15m")
CONTEXT_TOKENS = int(os.environ.get("WATERFREE_VISION_CONTEXT", "8192"))

# Load on a shared host is bursty: a call that times out while seven test
# suites hammer the GPU often succeeds a minute later. Retries are few and
# bounded because each one can cost a full --timeout; MAX_RETRIES caps a
# mistyped environment value.
MAX_RETRIES = 3
DEFAULT_RETRIES = max(0, min(MAX_RETRIES, int(os.environ.get("WATERFREE_VISION_RETRIES", "1"))))
RETRY_BACKOFF_SECONDS = 2.0
# 503 is Ollama's "server busy, maximum pending requests exceeded".
_RETRYABLE_HTTP = frozenset({503})

CLASSIFY = "classify"
# Purposes whose answer is a judgement: callers must not treat it as acceptance
# on its own, and the output says so.
ADVISORY_PURPOSES: frozenset[str] = frozenset({"compare", "triage", "ui", CLASSIFY})


class VisionError(RuntimeError):
    """The image could not be read, or the model could not be reached."""


class VisionTimeout(VisionError):
    """Ollama never answered within the time limit, on every attempt."""


# Indirection so tests can skip the real backoff wait.
_sleep = time.sleep


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
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int | None = None,
    labels: str | list[str] | None = None,
) -> dict:
    """Analyze images and return a JSON-shaped result.

    Multiple images go into a single turn, which is what makes `compare` work:
    the model sees them together rather than one at a time with no memory.

    `timeout` bounds each attempt at the model; a timed-out or busy attempt is
    retried `retries` times (default $WATERFREE_VISION_RETRIES) with backoff.
    `labels` (comma list or sequence) is required by, and only valid for, the
    `classify` purpose.
    """
    if not image_paths:
        raise ValueError("No image supplied.")
    if len(image_paths) > MAX_IMAGES:
        raise ValueError(
            f"{len(image_paths)} images supplied; the limit is {MAX_IMAGES}. "
            "More than that exceeds the context of a local vision model and "
            "produces a summary of nothing in particular."
        )
    timeout = _check_timeout(timeout)
    retries = DEFAULT_RETRIES if retries is None else _check_retries(retries)

    # Every usage error is raised before Ollama is contacted.
    resolved_purpose, user_labels = check_request(purpose, labels)
    is_classify = resolved_purpose.key == CLASSIFY
    label_order: list[str] = []
    if is_classify:
        if len(image_paths) != 1:
            raise ValueError(
                "--purpose classify takes exactly one image; run it once per image "
                "so each gets its own label."
            )
        label_order = classify.presentation_order(user_labels)

    route = resolve_route(resolved_purpose, override=model, tier=tier, question=question)
    model_id = route.model_id
    warnings: list[dict] = []
    leading = leading_question_warning(question, classify=is_classify)
    if leading:
        warnings.append(leading)
    # An explicit --model skips the pre-flight listing: it is the caller's
    # assertion that the model exists, and on a busy host the listing is the
    # call that times out. A wrong id still fails clearly -- the chat call's
    # 404 is turned into the same "not downloaded" error below.
    if not model.strip():
        list_timeout = min(LIST_TIMEOUT_SECONDS, timeout)
        if ensure_available(model_id, base=base, timeout=list_timeout) == UNVERIFIED:
            warnings.append({
                "code": "model-check-skipped",
                "message": (
                    f"Ollama did not list its models within {list_timeout:g} s (the host "
                    f"may be under load), so '{model_id}' was sent without confirming "
                    "it is downloaded."
                ),
            })

    prepared = [prepare_image(path) for path in image_paths]
    if is_classify:
        prompt = ""  # classify.classify builds its own prompts
        # Budget for the (longer) stricter-retry prompt so either attempt fits.
        budget_prompt = classify.build_prompt(
            question, label_order, retry_reason="x" * 120,
            default_question=resolved_purpose.default_question,
        )
    else:
        prompt = budget_prompt = _build_prompt(resolved_purpose, question, prepared)
    # Qwen image tokens use approximately one token per 28x28 pixel patch.
    # Leave headroom for wrappers, prompt text, and generated output.
    overhead = (len(budget_prompt) + len(resolved_purpose.system)) // 3 + 1536
    image_budget = CONTEXT_TOKENS - overhead
    image_tokens = sum(((p.width + 27) // 28) * ((p.height + 27) // 28) for p in prepared)
    estimated_tokens = image_tokens + overhead
    if estimated_tokens > CONTEXT_TOKENS:
        if image_budget <= 0:
            raise VisionError("Prompt leaves no image context. Shorten the question or increase WATERFREE_VISION_CONTEXT.")
        # A safety margin accommodates patch rounding and model wrappers.
        edge = int(MAX_EDGE_PX * math.sqrt(image_budget / image_tokens) * 0.85)
        if edge < 128:
            raise VisionError("Image context is too small for legible comparison. Compare fewer images or increase WATERFREE_VISION_CONTEXT.")
        prepared = [prepare_image(path, max_edge=edge) for path in image_paths]

    call = functools.partial(
        _generate,
        model_id=model_id,
        system=resolved_purpose.system,
        images=[item.b64 for item in prepared],
        timeout=timeout,
        retries=retries,
        base=base,
    )
    result: dict = {
        "purpose": resolved_purpose.key,
        "model": model_id,
        "tier": route.tier or None,
        "tierReason": route.reason,
        "question": question.strip() or resolved_purpose.default_question,
    }
    if is_classify:
        verdict = classify.classify(
            lambda text: call(
                prompt=text,
                # Deterministic for a given label order, so a gate re-run on the
                # same render and order gives the same answer.
                options={"num_ctx": CONTEXT_TOKENS, "temperature": 0},
                response_format=classify.response_schema(label_order),
            ),
            order=label_order,
            question=question,
            default_question=resolved_purpose.default_question,
        )
        warnings.extend(verdict.pop("warnings"))
        result.update({
            "labels": user_labels + list(classify.RESERVED),
            "labelOrder": label_order,
            **verdict,
        })
    else:
        result["answer"] = call(prompt=prompt, options={"num_ctx": CONTEXT_TOKENS}).strip()

    result.update({
        "advisory": resolved_purpose.key in ADVISORY_PURPOSES,
        "warnings": warnings,
        "images": [
            {
                "path": item.path,
                "sent": f"{item.width}x{item.height}",
                "original": f"{item.original_width}x{item.original_height}",
                "downscaled": item.was_downscaled,
            }
            for item in prepared
        ],
    })
    return result


def check_request(purpose: str, labels: str | list[str] | None = None) -> tuple[Purpose, list[str]]:
    """Validate purpose and labels without touching Ollama: (purpose, parsed labels).

    Raises ValueError (a usage error) for an unknown purpose, classify without
    usable --labels, or --labels with any other purpose.
    """
    resolved = get_purpose(purpose)
    if resolved.key == CLASSIFY:
        return resolved, classify.parse_labels(labels)
    if labels:
        raise ValueError("--labels only applies to --purpose classify.")
    return resolved, []


def _generate(
    *,
    model_id: str,
    system: str,
    prompt: str,
    images: list[str],
    timeout: float,
    retries: int,
    base: str = "",
    options: dict | None = None,
    response_format: str | dict | None = None,
) -> str:
    """One model answer, retrying load-shaped failures with backoff.

    Only failures that say "busy" are retried -- a timeout, or Ollama's 503
    queue-full. Anything else (daemon down, bad request) fails at once, and a
    404 for an unknown model becomes the actionable pull hint.
    """
    kwargs: dict = {"base": base} if base else {}
    if response_format is not None:
        kwargs["response_format"] = response_format
    attempts = retries + 1
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        try:
            return ollama_client.chat(
                model=model_id,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                images=images,
                timeout=timeout,
                keep_alive=KEEP_ALIVE,
                options=options,
                **kwargs,
            )
        except ollama_client.OllamaTimeout as exc:
            failure: ollama_client.OllamaError = exc
        except ollama_client.OllamaHTTPError as exc:
            if exc.status == 404 and _is_unknown_model(exc.detail):
                raise missing_model_error(model_id) from exc
            if exc.status not in _RETRYABLE_HTTP:
                raise VisionError(str(exc)) from exc
            failure = exc
        except ollama_client.OllamaError as exc:
            raise VisionError(str(exc)) from exc
        if attempt < attempts:
            _sleep(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise _load_error(
        failure, timeout=timeout, attempts=attempts, elapsed=time.monotonic() - started,
    ) from failure


def _is_unknown_model(detail: str) -> bool:
    # Ollama: {"error":"model 'x' not found"}. A bare "404 page not found" means
    # a wrong base URL, not a missing model, so "model" must be present too.
    lowered = detail.lower()
    return "model" in lowered and "not found" in lowered


def _load_error(
    failure: ollama_client.OllamaError, *, timeout: float, attempts: int, elapsed: float,
) -> VisionError:
    tries = f"{attempts} attempts, {elapsed:.0f} s in total" if attempts > 1 else "1 attempt"
    if isinstance(failure, ollama_client.OllamaTimeout):
        return VisionTimeout(
            f"Ollama did not answer in {timeout:g} s ({tries}); the host may be under "
            "load -- other models holding the GPU, or GPU/CPU-heavy processes such as "
            "test suites or game engines. Raise the limit with --timeout SECONDS "
            "(or $WATERFREE_VISION_TIMEOUT), try again when the host is quieter, or "
            "check what is resident with `ollama ps`."
        )
    return VisionError(
        f"Ollama stayed busy ({failure}; {tries}); the host may be under load. Try "
        "again when it is quieter, or allow more attempts with $WATERFREE_VISION_RETRIES."
    )


def _check_timeout(timeout: float) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = float("nan")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"--timeout must be a positive number of seconds, not {timeout!r}.")
    return value


def _check_retries(retries: int) -> int:
    if not isinstance(retries, int) or not 0 <= retries <= MAX_RETRIES:
        raise ValueError(f"retries must be between 0 and {MAX_RETRIES}, not {retries!r}.")
    return retries


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


def prepare_image(path: str, *, max_edge: int | None = None) -> PreparedImage:
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
    edge_limit = MAX_EDGE_PX if max_edge is None else max_edge
    if longest > edge_limit:
        scale = edge_limit / float(longest)
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
