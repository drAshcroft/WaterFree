"""Text to humanoid motion clips with Kimodo, under the exclusive GPU lease.

One command = one Kimodo worker process: take the GPU, start the worker (the
Kimodo checkpoint loads in seconds), run every clip through it, stop it,
release the GPU, then convert the clips to FBX with Blender (CPU only, so
after the GPU is free).

The text encoder is the expensive part: Llama-3-8B in bf16 on the CPU, ~100 s
to load and ~15.5 GB of RAM (it stays on the CPU by the user's decision; a
12 GB card cannot hold it unquantized). Each prompt's embedding is cached, so
the encoder loads only when a batch contains a prompt never encoded before --
and only then does the command insist on enough free RAM (todo ANIM-002).
"""

from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from backend.animgen import engine as kimodo
from backend.animgen import fbx as fbx_lib
from backend.animgen.config import (
    MAX_LOOP_SECONDS,
    MAX_SEGMENT_SECONDS,
    MAX_SEQUENCE_SECONDS,
    MAX_VARIANTS,
    AnimgenConfig,
    output_dir_for,
)
from backend.animgen.kimodo_worker import CONSTRAINT_TYPES, LOG_PREFIX, is_cached
from backend.engines.install import InstallError
from backend.engines.outputs import resolve_seed, stem, write_sidecar
from backend.gpu import lease as gpu_lease
from backend.gpu import ram
from backend.gpu.engine import EngineError, EngineServer
from backend.gpu.preflight import exclusive_gpu

MAX_CLIPS = 64
# Free VRAM to require: Kimodo-SOMA-RP's denoiser measured +1.5 GB above the
# desktop (nvidia-smi, 2026-11-18; torch peak allocation 1.13 GB) plus headroom.
VRAM_GB = 2.5
_REQUEST_TIMEOUT_SECONDS = 1800.0   # the first clip may include the ~100 s encoder load
_STARTUP_TIMEOUT_SECONDS = 600.0
_HEARTBEAT_SECONDS = 30.0
_ECHO = re.compile(r"error|warning|traceback|exception", re.IGNORECASE)

Report = Callable[[str], None]


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def make(
    clips: list[dict],
    *,
    count: int = 1,
    config: AnimgenConfig,
    workspace: str,
    output_dir: str = "",
    policy: gpu_lease.WaitPolicy | None = None,
    check_vram: bool = True,
    check_ram: bool = True,
    report: Report = _stderr,
) -> dict:
    config.validate()
    clips = [normalise_clip(clip, config, index) for index, clip in enumerate(clips, start=1)]
    if not clips:
        raise ValueError("Describe a motion, e.g. \"a person jogs forward\".")
    if not 1 <= count <= MAX_VARIANTS:
        raise ValueError(f"count must be between 1 and {MAX_VARIANTS}")
    if len(clips) * count > MAX_CLIPS:
        raise ValueError(f"At most {MAX_CLIPS} clips per command; split the batch.")
    marker = kimodo.require()
    blender = None if config.fbx == "never" else fbx_lib.find_blender(config.blender_path)
    if config.fbx == "always" and blender is None:
        raise InstallError("FBX output was requested (fbx: always) but Blender was not found. Install Blender, "
                           "set blenderPath in .waterfree/animgen.json, or use --fbx auto.")
    destination = Path(output_dir).expanduser() if output_dir else output_dir_for(config, workspace)
    destination.mkdir(parents=True, exist_ok=True)

    encoder = kimodo.encoder_id(marker)
    cache_dir = str(kimodo.embedding_cache_dir())
    texts = list(dict.fromkeys(s["text"] for clip in clips for s in clip["segments"]))
    new_texts = [t for t in texts if not is_cached(cache_dir, encoder, t)]
    base_seed = resolve_seed(config.seed)
    jobs = _plan(clips, count, base_seed, destination)

    files: list[dict] = []
    failures: list[dict] = []
    encoder_use = {"encoded": 0, "loadSeconds": None}
    memory = None
    started = time.monotonic()
    detail = f"{len(jobs)} clip(s), {len(new_texts)} new prompt(s)"
    with exclusive_gpu(label="animgen make", detail=detail, vram_gb=VRAM_GB, policy=policy,
                       check_vram=check_vram) as grant:
        if new_texts and check_ram:
            # After the lease and the Ollama eviction, so memory Ollama gave back counts.
            memory = ram.require(config.ram_gb, purpose=(
                f"Encoding {len(new_texts)} new prompt(s) with Kimodo's CPU text encoder (Llama-3-8B)"))
        elif new_texts:
            memory = ram.query()
        if new_texts:
            report(f"{len(new_texts)} prompt(s) not encoded before: the text encoder will load "
                   "(~100 s, ~15 GB of RAM). Prompts seen before are reused from the cache.")
        else:
            report("Every prompt is already encoded: the text encoder will not load.")
        server = EngineServer(name="kimodo", argv=kimodo.worker_argv(marker), cwd=kimodo.KIMODO.install_dir(),
                              env=kimodo.worker_env(), startup_timeout=_STARTUP_TIMEOUT_SECONDS,
                              report=_filtered(report), echo_log=True)
        with server:
            report(f"Kimodo ready in {server.load_seconds:.0f}s; generating {len(jobs)} clip(s)")
            for index, job in enumerate(jobs, start=1):
                try:
                    entry, answer = _one(server, job, index, len(jobs), config, marker, report)
                    files.append(entry)
                    encoder_use["encoded"] += answer.get("prompts", {}).get("encoded", 0)
                    if answer.get("encoderLoadSeconds") is not None:
                        encoder_use["loadSeconds"] = answer["encoderLoadSeconds"]
                except EngineError as exc:
                    failures.append({"clip": job["label"], "seed": job["seed"], "error": str(exc)})
                    if not server.running():
                        for rest in jobs[index:]:
                            failures.append({"clip": rest["label"], "seed": rest["seed"],
                                             "error": "skipped: the Kimodo worker crashed"})
                        break
        load_seconds, spill = server.load_seconds, server.spill_report()

    fbx_summary = _convert_fbx(files, blender, config, report)
    return {
        "count": len(files),
        "outputDir": str(destination),
        "files": files,
        "failed": failures,
        "fbx": fbx_summary,
        "textEncoder": {"newPrompts": len(new_texts), "encoded": encoder_use["encoded"],
                        "loadSeconds": encoder_use["loadSeconds"], "device": "cpu"},
        "settings": config.describe(),
        "gpu": grant.public(),
        "vramSpill": spill,
        "ram": memory.public() if memory else None,
        "timing": {"loadSeconds": round(load_seconds or 0, 1),
                   "totalSeconds": round(time.monotonic() - started, 1)},
        "licence": kimodo.KIMODO.licence,
    }


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

def normalise_clip(clip: dict | str, config: AnimgenConfig, index: int = 1) -> dict:
    """``"text"`` or ``{text|segments, seconds?, name?, seed?, inPlace?}`` -> one validated clip."""
    if isinstance(clip, str):
        clip = {"text": clip}
    if not isinstance(clip, dict):
        # ValueError, not TypeError: it is bad user input, which the CLI reports as a usage error (exit 2).
        raise ValueError(f"clip {index}: expected a prompt string or an object")  # noqa: TRY004
    raw_segments = clip.get("segments")
    if raw_segments is None:
        raw_segments = [{"text": clip.get("text", ""), "seconds": clip.get("seconds")}]
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError(f"clip {index}: \"segments\" must be a non-empty list")
    segments = []
    for number, segment in enumerate(raw_segments, start=1):
        if isinstance(segment, str):
            segment = {"text": segment}
        text = " ".join(str(segment.get("text", "")).split())
        if not text:
            raise ValueError(f"clip {index}, segment {number}: the prompt text is empty")
        seconds = float(segment.get("seconds") or config.seconds)
        if not 0.5 <= seconds <= MAX_SEGMENT_SECONDS:
            raise ValueError(f"clip {index}, segment {number}: seconds must be between 0.5 and "
                             f"{MAX_SEGMENT_SECONDS:g} (Kimodo is trained on clips up to 10 s; chain "
                             "segments for longer motion)")
        segments.append({"text": text, "seconds": seconds})
    total = sum(s["seconds"] for s in segments)
    if total > MAX_SEQUENCE_SECONDS:
        raise ValueError(f"clip {index}: {total:g} s in total; the limit is {MAX_SEQUENCE_SECONDS:g} s")
    out: dict = {"segments": segments}
    if clip.get("name"):
        name = file_stem(str(clip["name"]))
        if not name:
            raise ValueError(f"clip {index}: \"name\" has no usable characters")
        out["name"] = name
    if clip.get("seed") is not None:
        out["seed"] = int(clip["seed"])
    if clip.get("inPlace") is not None:
        out["inPlace"] = bool(clip["inPlace"])
    if clip.get("path") is not None:
        out["path"] = _validate_path(clip["path"], index)
    if clip.get("constraints") is not None:
        out["constraints"] = load_constraints(clip["constraints"], index)
    loop = bool(clip.get("loop", config.loop))
    if loop:
        if len(segments) != 1:
            raise ValueError(f"clip {index}: a loop is one prompt, not a sequence of segments")
        if segments[0]["seconds"] > MAX_LOOP_SECONDS:
            raise ValueError(f"clip {index}: a loop is at most {MAX_LOOP_SECONDS:g} s")
        if out.get("path") or out.get("constraints"):
            raise ValueError(f"clip {index}: a loop cannot also have a path or constraints (the cycle is cut "
                             "from a longer generation, so their frame numbers would not line up)")
    out["loop"] = loop
    return out


def _validate_path(path, index: int) -> list[list[float]]:
    """Waypoints ``[[x, z], ...]`` in metres; the clip starts at (0, 0) facing +z, +x to the character's left."""
    if not isinstance(path, list) or not path:
        raise ValueError(f"clip {index}: \"path\" must be a non-empty list of [x, z] points (metres)")
    points = []
    for point in path:
        try:
            x, z = (float(v) for v in point)
        except (TypeError, ValueError):
            raise ValueError(f"clip {index}: path point {point!r} is not [x, z]") from None
        if not (math.isfinite(x) and math.isfinite(z)) or max(abs(x), abs(z)) > 100:
            raise ValueError(f"clip {index}: path point {point!r} is out of range (|x|, |z| <= 100 m)")
        points.append([x, z])
    if all(p == [0.0, 0.0] for p in points):
        raise ValueError(f"clip {index}: the path does not go anywhere (the clip already starts at 0, 0)")
    return points


def load_constraints(source, index: int = 1) -> list[dict]:
    """A Kimodo constraints list (the JSON its demo exports): a file path or the list itself."""
    if isinstance(source, str):
        path = Path(source).expanduser()
        try:
            source = json.loads(path.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise ValueError(f"clip {index}: cannot read constraints file {path}: {exc}") from exc
        except ValueError as exc:
            raise ValueError(f"clip {index}: constraints file {path} is not valid JSON: {exc}") from exc
    if not isinstance(source, list) or not source:
        raise ValueError(f"clip {index}: constraints must be a non-empty JSON list of Kimodo constraints")
    for number, item in enumerate(source, start=1):
        kind = item.get("type") if isinstance(item, dict) else None
        if kind not in CONSTRAINT_TYPES:
            raise ValueError(f"clip {index}: constraint {number} has type {kind!r}; Kimodo knows "
                             f"{', '.join(CONSTRAINT_TYPES)}")
        if "frame_indices" not in item:
            raise ValueError(f"clip {index}: constraint {number} ({kind}) has no frame_indices")
    return source


def parse_clips_file(path: str) -> list[dict]:
    """Clips from a .txt (one prompt per line, ``#`` comments) or a .json list.

    JSON entries are prompt strings or objects::

        {"name": "orc_idle", "text": "a heavy creature stands, breathing", "seconds": 4,
         "seed": 3, "inPlace": true}
        {"name": "draw_and_swing", "segments": [
            {"text": "a person draws a sword", "seconds": 1.5},
            {"text": "a person swings the sword", "seconds": 2}]}

    ``name`` becomes the output file stem (as written, apart from characters a
    file name cannot hold); names must be unique within the file.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"Prompts file does not exist: {path}")
    text = source.read_text(encoding="utf-8-sig")
    if source.suffix.lower() != ".json":
        return [{"text": line.strip()} for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"{path} must hold a JSON list of clips")  # noqa: TRY004 - user input; exit 2
    for item in raw:
        # A constraints file named in the batch file is relative to the batch file, not to the cwd.
        if isinstance(item, dict) and isinstance(item.get("constraints"), str):
            named = Path(item["constraints"]).expanduser()
            item["constraints"] = str(named if named.is_absolute() else source.parent / named)
    names = [file_stem(str(item["name"])) for item in raw if isinstance(item, dict) and item.get("name")]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"{path}: these names are used more than once and would overwrite each other: "
                         f"{', '.join(duplicates)}")
    return raw


def file_stem(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip(".-")[:80]


def _plan(clips: list[dict], count: int, base_seed: int, destination: Path) -> list[dict]:
    jobs = []
    serial = 0
    for clip in clips:
        prompt = " / ".join(s["text"] for s in clip["segments"])
        for variant in range(count):
            seed = clip["seed"] + variant if "seed" in clip else base_seed + serial
            serial += 1
            if clip.get("name"):
                name = clip["name"] if count == 1 else f"{clip['name']}_{variant + 1:02d}"
            else:
                name = stem(prompt if count == 1 else f"{prompt} {variant + 1}")
            jobs.append({"clip": clip, "seed": seed, "name": name, "label": clip.get("name") or prompt[:60],
                         "bvh": destination / f"{name}.bvh", "npz": destination / f"{name}.npz"})
    return jobs


# ---------------------------------------------------------------------------
# One clip
# ---------------------------------------------------------------------------

def _one(server: EngineServer, job: dict, index: int, total: int, config: AnimgenConfig, marker: dict,
         report: Report) -> tuple[dict, dict]:
    """Generate one clip; return (result entry, the worker's answer)."""
    clip = job["clip"]
    in_place = clip.get("inPlace", config.in_place)
    prompt = " / ".join(s["text"] for s in clip["segments"])
    preview = job["bvh"].with_suffix(".png") if config.preview else None
    request = {
        "segments": clip["segments"],
        "seed": job["seed"],
        "steps": config.steps,
        "transitionFrames": config.transition_frames,
        "postprocess": config.postprocess,
        "inPlace": in_place,
        "standardTpose": config.tpose == "standard",
        "bvh": str(job["bvh"]),
        "npz": str(job["npz"]),
    }
    if clip.get("loop"):
        request["loop"] = {"seconds": clip["segments"][0]["seconds"]}
    if clip.get("path"):
        request["path"] = clip["path"]
    if clip.get("constraints"):
        request["constraints"] = clip["constraints"]
    if preview is not None:
        request["preview"] = str(preview)
        request["title"] = f"{prompt}  (seed {job['seed']}{', loop' if clip.get('loop') else ''}" \
                           f"{', in place' if in_place else ''})"
    report(f"[{index}/{total}] {job['label']} (seed {job['seed']})")
    stop = threading.Event()
    begun = time.monotonic()

    def heartbeat() -> None:
        while not stop.wait(_HEARTBEAT_SECONDS):
            report(f"[{index}/{total}] still working, {int(time.monotonic() - begun)}s elapsed")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        body, _headers = server.post_json("/generate", request, timeout=_REQUEST_TIMEOUT_SECONDS)
    finally:
        stop.set()
        thread.join(timeout=1)
    try:
        answer = json.loads(body)
    except ValueError as exc:
        raise EngineError(f"Kimodo returned something that is not JSON for {job['label']}",
                          log_path=server.log_path, log_tail=server.log_tail()) from exc
    if not job["bvh"].is_file() or not job["npz"].is_file():
        raise EngineError(f"Kimodo reported success but did not write {job['bvh'].name}",
                          log_path=server.log_path, log_tail=server.log_tail())
    stats = answer.get("stats", {})
    revisions = marker.get("revisions", {})
    files = {"bvh": str(job["bvh"]), "npz": str(job["npz"])}
    if answer.get("preview"):
        files["preview"] = answer["preview"]
    metadata = {
        "kind": "motion",
        "prompt": prompt,
        "segments": clip["segments"],
        "seed": job["seed"],
        "settings": {**config.describe(), "inPlace": in_place, "loop": bool(clip.get("loop"))},
        "stats": stats,
        "loop": answer.get("loop"),
        "constraints": {"count": answer.get("constraints", 0), "path": clip.get("path"),
                        "kimodo": clip.get("constraints")} if answer.get("constraints") else None,
        "skeleton": {**answer.get("skeleton", {}), "format": "BVH in centimetres, Y up",
                     "restPose": "T-pose" if config.tpose == "standard" else "BONES-SEED rest pose"},
        "files": files,
        "seconds": answer.get("seconds"),
        "textEncoder": {"cached": answer.get("prompts", {}).get("cached"),
                        "encoded": answer.get("prompts", {}).get("encoded"),
                        "encodeSeconds": answer.get("encodeSeconds"), "device": "cpu", "dtype": "bfloat16"},
        "engine": {"project": kimodo.KIMODO.project, "version": kimodo.KIMODO.version,
                   "commit": kimodo.KIMODO.commit},
        "model": {"key": answer.get("model"), "repo": "nvidia/Kimodo-SOMA-RP-v1.2",
                  "revision": revisions.get("nvidia/Kimodo-SOMA-RP-v1.2")},
        "textEncoderWeights": {repo: rev for repo, rev in revisions.items() if repo != "nvidia/Kimodo-SOMA-RP-v1.2"},
        "licence": kimodo.KIMODO.licence,
    }
    sidecar = write_sidecar(job["bvh"], metadata)
    loop = answer.get("loop")
    shape = (f"loop of {loop['cycleSeconds']}s, seam {loop['seamStepM'] * 100:.1f} cm vs typical step "
             f"{loop['typicalStepM'] * 100:.1f} cm" if loop else f"loop gap {stats.get('loopGapM')} m")
    report(f"[{index}/{total}] {stats.get('frames')} frames, travel {stats.get('rootTravelM')} m, {shape}, "
           f"{answer.get('seconds')}s -> {job['bvh'].name}")
    entry = {
        "bvh": str(job["bvh"]), "npz": str(job["npz"]), "metadata": str(sidecar),
        "prompt": prompt, "seed": job["seed"], "frames": stats.get("frames"),
        "seconds": stats.get("seconds"), "rootTravelM": stats.get("rootTravelM"), "loopGapM": stats.get("loopGapM"),
        "inPlace": in_place, "generateSeconds": answer.get("seconds"),
    }
    if answer.get("preview"):
        entry["preview"] = answer["preview"]
    if loop:
        entry["loop"] = {k: loop[k] for k in ("cycleSeconds", "seamStepM", "typicalStepM", "poseErrorM")}
    return entry, answer


def _convert_fbx(files: list[dict], blender: Path | None, config: AnimgenConfig, report: Report) -> dict:
    if config.fbx == "never":
        return {"converted": 0, "skipped": "fbx is set to never"}
    if blender is None:
        return {"converted": 0, "skipped": "Blender was not found; BVH only (set blenderPath to enable FBX)"}
    if not files:
        return {"converted": 0, "blender": str(blender)}
    jobs = [{"bvh": f["bvh"], "fbx": str(Path(f["bvh"]).with_suffix(".fbx")), "name": Path(f["bvh"]).stem}
            for f in files]
    report(f"Converting {len(jobs)} clip(s) to FBX with {blender}")
    results = fbx_lib.convert(jobs, blender)
    converted = 0
    errors = []
    for entry, result in zip(files, results, strict=True):
        if result.get("ok"):
            entry["fbx"] = result["fbx"]
            converted += 1
            _add_to_sidecar(entry["metadata"], result["fbx"],
                            {"fps": result.get("fps"), "bones": result.get("bones"), "blender": str(blender)})
        else:
            errors.append({"bvh": entry["bvh"], "error": result.get("error")})
    summary: dict = {"converted": converted, "blender": str(blender)}
    if errors:
        summary["errors"] = errors
    return summary


def _add_to_sidecar(path: str, fbx_path: str, details: dict) -> None:
    sidecar = Path(path)
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data.setdefault("files", {})["fbx"] = fbx_path
    data["fbx"] = details
    sidecar.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _filtered(report: Report) -> Report:
    """Pass through the worker's own progress lines and anything that sounds like trouble."""
    def emit(message: str) -> None:
        if not message.startswith("[kimodo]") or LOG_PREFIX in message or _ECHO.search(message):
            report(message.replace(f"[kimodo] {LOG_PREFIX} ", "kimodo: "))
    return emit
