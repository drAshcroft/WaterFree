"""Image(s) to UV-textured GLB with trellis.cpp, under the exclusive GPU lease.

One command = one trellis-server process: take the GPU, start the server, run
every image through it (the ~20 s model load is paid once), stop the server,
release the GPU. Each GLB gets a sidecar with the input's hash, the seed, the
settings and the engine/model provenance.
"""

from __future__ import annotations

import json
import os
import re
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from backend.engines import install
from backend.engines.outputs import (
    engine_provenance,
    file_sha256,
    resolve_seed,
    stem,
    write_sidecar,
)
from backend.gpu import lease as gpu_lease
from backend.gpu.engine import EngineError, EngineServer
from backend.gpu.preflight import exclusive_gpu
from backend.meshgen.config import MeshgenConfig, output_dir_for

MAX_IMAGES = 16
INSTALL_HINT = "waterfree meshgen install"

# Free VRAM to require, by resolution: measured peak above the desktop's own
# use on an RTX 3060 12 GB (trellis.cpp v0.8.1, Q8_0 weights, 2026-11-17) plus
# ~0.5 GB headroom. 512 peaked at 2.5 GB, 1024 at 4.5 GB. 1536 is not measured;
# the figure is an extrapolation and deliberately generous.
VRAM_GB_BY_RESOLUTION = {512: 3.0, 1024: 5.0, 1536: 8.0}
_REQUEST_TIMEOUT_SECONDS = 3 * 3600   # a heavy res-1536 mesh on a small card can take an hour
_HEARTBEAT_SECONDS = 30.0

# Lines from trellis-server worth passing through: its "[3/7] ..." stage
# boundaries, the sampler's progress bar, and anything that sounds like trouble.
# Everything else is per-graph chatter.
_ECHO_PATTERN = re.compile(r"\[\d+/\d+\]|\[flow\]|error|warning|fail", re.IGNORECASE)

Report = Callable[[str], None]


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def make(
    images: list[str],
    *,
    config: MeshgenConfig,
    workspace: str,
    output_dir: str = "",
    policy: gpu_lease.WaitPolicy | None = None,
    check_vram: bool = True,
    report: Report = _stderr,
) -> dict:
    config.validate()
    if not images:
        raise ValueError("Give at least one input image.")
    if len(images) > MAX_IMAGES:
        raise ValueError(f"At most {MAX_IMAGES} images per command.")
    inputs = [_validate_image(path) for path in images]
    release, model_set = install.require("trellis", config.model_set, install_hint=INSTALL_HINT)
    destination = Path(output_dir).expanduser() if output_dir else output_dir_for(config, workspace)
    destination.mkdir(parents=True, exist_ok=True)

    argv = [str(release.executable("server")), "--models", str(model_set.install_dir()), "--gpu", "0",
            "--res", str(config.resolution), "--steps", str(config.steps)]
    if config.atlas:
        argv += ["--atlas", str(config.atlas)]
    if not config.texture:
        argv.append("--no-texture")
    if config.uv == "box":
        argv.append("--box-uv")
    if config.copyright.strip():
        argv += ["--copyright", config.copyright.strip()]

    detail = f"{model_set.key}, res {config.resolution}, {len(inputs)} image(s)"
    results: list[dict] = []
    failures: list[dict] = []
    started = time.monotonic()
    need = VRAM_GB_BY_RESOLUTION.get(config.resolution, model_set.vram_gb)
    with exclusive_gpu(label="meshgen make", detail=detail, vram_gb=need,
                       policy=policy, check_vram=check_vram) as grant:
        server = EngineServer(name="trellis", argv=argv, cwd=release.install_dir(),
                              env={**os.environ, **release.env}, report=_filtered(report), echo_log=True)
        with server:
            report(f"trellis ready in {server.load_seconds:.0f}s; generating {len(inputs)} mesh(es)")
            for index, source in enumerate(inputs, start=1):
                try:
                    results.append(_one(server, source, index, len(inputs), config, destination,
                                        release, model_set, report))
                except EngineError as exc:
                    failures.append({"image": str(source), "error": str(exc)})
                    if not server.running():
                        # A crashed server takes the rest of the batch with it.
                        for rest in inputs[index:]:
                            failures.append({"image": str(rest), "error": "skipped: trellis crashed"})
                        break
        load_seconds = server.load_seconds

    spill = server.spill_report()
    return {
        "count": len(results),
        "outputDir": str(destination),
        "files": [{"mesh": r["mesh"], "metadata": r["metadata"], "input": r["input"],
                   "seed": r["seed"], "triangles": r["stats"].get("triangles"),
                   "seconds": r["seconds"]} for r in results],
        "failed": failures,
        "settings": config.describe(),
        "gpu": grant.public(),
        "vramSpill": spill,
        "timing": {"loadSeconds": round(load_seconds or 0, 1),
                   "totalSeconds": round(time.monotonic() - started, 1)},
        "licence": model_set.licence,
    }


def _one(server: EngineServer, source: Path, index: int, total: int, config: MeshgenConfig,
         destination: Path, release, model_set, report: Report) -> dict:
    seed = resolve_seed(config.seed)
    # v0.8.1's server reads seed/resolution/bg_removal/uv/band/webp per request
    # (src/trellis-server.cpp); "faces" is documented upstream but not read.
    fields = {"seed": str(seed), "resolution": str(config.resolution)}
    if config.bg_removal != "auto":
        fields["bg_removal"] = config.bg_removal
    report(f"[{index}/{total}] {source.name}: generating (seed {seed})")
    stop = threading.Event()
    beat_started = time.monotonic()

    def heartbeat() -> None:
        while not stop.wait(_HEARTBEAT_SECONDS):
            report(f"[{index}/{total}] {source.name}: still generating, "
                   f"{int(time.monotonic() - beat_started)}s elapsed")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        body, _headers = server.post_multipart("/generate", fields=fields, files={"image": source},
                                               timeout=_REQUEST_TIMEOUT_SECONDS)
    finally:
        stop.set()
        thread.join(timeout=1)
    seconds = time.monotonic() - beat_started
    if body[:4] != b"glTF":
        raise EngineError(f"trellis returned {len(body)} bytes that are not a GLB for {source.name}",
                          log_path=server.log_path, log_tail=server.log_tail())
    mesh_path = destination / f"{stem(source.stem)}.glb"
    mesh_path.write_bytes(body)
    stats = glb_stats(body)
    metadata = {
        "input": {"path": str(source), "sha256": file_sha256(source)},
        "seed": seed,
        "settings": config.describe(),
        "requestFields": fields,
        "stats": stats,
        "seconds": round(seconds, 1),
        **engine_provenance(release, model_set),
    }
    sidecar = write_sidecar(mesh_path, metadata)
    report(f"[{index}/{total}] {source.name}: {stats.get('triangles')} triangles in {seconds:.0f}s -> {mesh_path.name}")
    return {"mesh": str(mesh_path), "metadata": str(sidecar), "input": str(source), "seed": seed,
            "stats": stats, "seconds": round(seconds, 1)}


def _validate_image(path: str) -> Path:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Input image does not exist: {path}")
    try:
        from PIL import Image
        with Image.open(source) as image:
            image.verify()
    except ImportError:
        pass
    except Exception as exc:
        raise ValueError(f"Not a readable image: {path} ({exc})") from exc
    return source


def _filtered(report: Report) -> Report:
    """Pass through only the trellis log lines a person would want to see."""
    def emit(message: str) -> None:
        if not message.startswith("[trellis]") or _ECHO_PATTERN.search(message):
            report(message)
    return emit


def glb_stats(data: bytes) -> dict:
    """Triangle/vertex/material counts from a GLB's JSON chunk, without a 3D library."""
    try:
        magic, _version, _length = struct.unpack_from("<4sII", data, 0)
        if magic != b"glTF":
            return {}
        chunk_length, chunk_type = struct.unpack_from("<II", data, 12)
        if chunk_type != 0x4E4F534A:  # "JSON"
            return {}
        gltf = json.loads(data[20:20 + chunk_length])
    except (struct.error, ValueError):
        return {}
    accessors = gltf.get("accessors", [])
    triangles = vertices = 0
    for mesh in gltf.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            if primitive.get("mode", 4) != 4:
                continue
            if "indices" in primitive and primitive["indices"] < len(accessors):
                triangles += accessors[primitive["indices"]].get("count", 0) // 3
            position = primitive.get("attributes", {}).get("POSITION")
            if position is not None and position < len(accessors):
                vertices += accessors[position].get("count", 0)
    return {
        "triangles": triangles,
        "vertices": vertices,
        "materials": len(gltf.get("materials", [])),
        "textures": len(gltf.get("textures", [])),
        "bytes": len(data),
        "extensionsUsed": gltf.get("extensionsUsed", []),
    }
