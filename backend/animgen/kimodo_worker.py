"""Kimodo engine process for `waterfree animgen` -- runs in the Kimodo venv, not WaterFree's.

    python -I kimodo_worker.py serve --model NAME --cache-dir DIR --encoder-id ID --host H --port P

Only the standard library is imported at module level, so WaterFree's own
process can import this file for :func:`embedding_path` and agree with the
worker on the cache layout without having torch or Kimodo installed.

Text-embedding cache
--------------------
Kimodo conditions on a 4096-d LLM2Vec embedding of each prompt, computed by
Llama-3-8B in bf16 on the CPU (~15.5 GB of RAM and ~100 s to load). The
embedding of a given text never changes for a given encoder, so it is stored
once per text and the encoder is loaded only when a prompt has never been seen.
Re-rolling a clip with a new seed, or a new duration, costs no encoder at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

EMBEDDING_DIM = 4096
LOG_PREFIX = "animgen:"


def embedding_key(encoder_id: str, text: str) -> str:
    return hashlib.sha256(f"{encoder_id}\n{text}".encode()).hexdigest()


def embedding_path(cache_dir: str, encoder_id: str, text: str) -> str:
    key = embedding_key(encoder_id, text)
    return os.path.join(cache_dir, key[:2], f"{key}.npy")


def is_cached(cache_dir: str, encoder_id: str, text: str) -> bool:
    """True when ``text`` needs no encoder: cached already, or empty (Kimodo zeroes empty prompts)."""
    return not text.strip() or os.path.isfile(embedding_path(cache_dir, encoder_id, text))


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


# ---------------------------------------------------------------------------
# Motion helpers (numpy only; unit-tested from WaterFree's venv)
# ---------------------------------------------------------------------------

def motion_stats(motion: dict, fps: float) -> dict:
    """Frames, travel, and how far the last pose is from the first with travel removed."""
    import numpy as np

    joints = np.asarray(motion["posed_joints"], dtype=np.float64)       # [T, J, 3], metres, y up
    root = np.asarray(motion["root_positions"], dtype=np.float64)       # [T, 3]
    frames = int(joints.shape[0])
    steps = np.diff(root[:, [0, 2]], axis=0)
    local = joints.copy()
    local[:, :, 0] -= root[:, None, 0]
    local[:, :, 2] -= root[:, None, 2]
    return {
        "frames": frames,
        "fps": float(fps),
        "seconds": round(frames / fps, 3),
        "rootTravelM": round(float(np.linalg.norm(steps, axis=1).sum()), 3),
        "rootDisplacementM": round(float(np.linalg.norm(root[-1, [0, 2]] - root[0, [0, 2]])), 3),
        # Mean joint distance between the first and last pose, travel removed:
        # a few centimetres means the clip can be looped with a short blend.
        "loopGapM": round(float(np.linalg.norm(local[-1] - local[0], axis=1).mean()), 3),
    }


def make_in_place(motion: dict) -> dict:
    """Remove the travel: subtract the smoothed root's x/z from every position.

    Uses Kimodo's ``smooth_root_pos`` rather than the pelvis itself, so the hips
    keep their natural side-to-side sway while the clip no longer moves across
    the floor. Turning (heading) is kept.
    """
    import numpy as np

    out = dict(motion)
    smooth = np.asarray(motion["smooth_root_pos"], dtype=np.float32)
    dx, dz = smooth[:, 0].copy(), smooth[:, 2].copy()
    joints = np.array(motion["posed_joints"], dtype=np.float32, copy=True)
    joints[:, :, 0] -= dx[:, None]
    joints[:, :, 2] -= dz[:, None]
    out["posed_joints"] = joints
    for key in ("root_positions", "smooth_root_pos"):
        values = np.array(motion[key], dtype=np.float32, copy=True)
        values[:, 0] -= dx
        values[:, 2] -= dz
        out[key] = values
    return out


# ---------------------------------------------------------------------------
# Loops: cut a cycle at the best-matching frame pair, then blend the seam (ANIM-003)
# ---------------------------------------------------------------------------

LOOP_VELOCITY_WEIGHT_S = 0.1    # 1 m/s of velocity mismatch costs as much as 10 cm of pose mismatch
LOOP_MAX_BLEND_FRAMES = 8


def loop_generation_seconds(cycle_seconds: float) -> float:
    """How long to generate so a cycle of about ``cycle_seconds`` can be found inside it."""
    return round(min(10.0, cycle_seconds * 1.4 + 1.5), 2)


def _yaw(rotations):
    """Yaw (radians) of each rotation's body-fixed +z axis; [..., 3, 3] -> [...]."""
    import numpy as np

    forward = rotations[..., :, 2]
    return np.arctan2(forward[..., 0], forward[..., 2])


def _rot_y(angle):
    """Rotation about +y taking +z towards +x by ``angle``; [...] -> [..., 3, 3]."""
    import numpy as np

    c, s = np.cos(angle), np.sin(angle)
    zero, one = np.zeros_like(c), np.ones_like(c)
    return np.stack([np.stack([c, zero, s], -1), np.stack([zero, one, zero], -1),
                     np.stack([-s, zero, c], -1)], -2)


def _loop_features(local_rot_mats, root_positions, posed_joints, fps: float):
    """Per-frame pose (root-relative joints, facing normalised) and velocity in m/s."""
    import numpy as np

    yaw = _yaw(local_rot_mats[:, 0])
    unturn = _rot_y(-yaw)                                     # [T, 3, 3]
    rel = posed_joints - root_positions[:, None, :] * np.array([1.0, 0.0, 1.0])
    pose = np.einsum("tab,tjb->tja", unturn, rel)             # [T, J, 3]
    step = np.diff(pose, axis=0, append=pose[-1:]) * fps
    root_step = np.diff(root_positions, axis=0, append=root_positions[-1:]) * fps
    root_vel = np.einsum("tab,tb->ta", unturn, root_step)     # travel velocity in the body's own frame
    velocity = np.concatenate([step, root_vel[:, None, :]], axis=1)
    return pose, velocity


def find_loop(motion: dict, fps: float, target_frames: int) -> dict:
    """The frame pair (start, end) whose poses and velocities match best, ``end - start`` near the target."""
    import numpy as np

    rots = np.asarray(motion["local_rot_mats"], dtype=np.float64)
    root = np.asarray(motion["root_positions"], dtype=np.float64)
    joints = np.asarray(motion["posed_joints"], dtype=np.float64)
    frames = rots.shape[0]
    pose, velocity = _loop_features(rots, root, joints, fps)
    pose_cost = np.linalg.norm(pose[:, None] - pose[None, :], axis=-1).mean(-1)          # [T, T]
    vel_cost = np.linalg.norm(velocity[:, None] - velocity[None, :], axis=-1).mean(-1)
    cost = pose_cost + LOOP_VELOCITY_WEIGHT_S * vel_cost
    lo = max(8, round(target_frames * 0.6))
    hi = max(lo, round(target_frames * 1.4))
    blend = min(LOOP_MAX_BLEND_FRAMES, max(2, lo // 4))
    start, end = np.meshgrid(np.arange(frames), np.arange(frames), indexing="ij")
    length = end - start
    valid = (start >= blend) & (length >= lo) & (length <= hi) & (end <= frames - 1)
    if not valid.any():
        raise ValueError(f"a {frames}-frame clip is too short to find a ~{target_frames}-frame loop in")
    masked = np.where(valid, cost, np.inf)
    i, j = np.unravel_index(int(np.argmin(masked)), masked.shape)
    return {"start": int(i), "end": int(j), "blend": int(blend), "poseErrorM": round(float(pose_cost[i, j]), 4),
            "velocityError": round(float(vel_cost[i, j]), 4)}


def _mat_to_quat(m):
    """[..., 3, 3] rotation matrices -> [..., 4] unit quaternions (w, x, y, z)."""
    import numpy as np

    m = np.asarray(m, dtype=np.float64)
    trace = m[..., 0, 0] + m[..., 1, 1] + m[..., 2, 2]
    candidates = np.stack([
        np.stack([1 + trace, m[..., 2, 1] - m[..., 1, 2], m[..., 0, 2] - m[..., 2, 0], m[..., 1, 0] - m[..., 0, 1]], -1),
        np.stack([m[..., 2, 1] - m[..., 1, 2], 1 + m[..., 0, 0] - m[..., 1, 1] - m[..., 2, 2],
                  m[..., 0, 1] + m[..., 1, 0], m[..., 0, 2] + m[..., 2, 0]], -1),
        np.stack([m[..., 0, 2] - m[..., 2, 0], m[..., 0, 1] + m[..., 1, 0],
                  1 - m[..., 0, 0] + m[..., 1, 1] - m[..., 2, 2], m[..., 1, 2] + m[..., 2, 1]], -1),
        np.stack([m[..., 1, 0] - m[..., 0, 1], m[..., 0, 2] + m[..., 2, 0], m[..., 1, 2] + m[..., 2, 1],
                  1 - m[..., 0, 0] - m[..., 1, 1] + m[..., 2, 2]], -1),
    ], -2)                                                     # [..., 4 candidates, 4]
    # Each candidate is the quaternion scaled by 4*q_k; use the best-conditioned one.
    best = np.argmax(np.stack([1 + trace, 1 + m[..., 0, 0] - m[..., 1, 1] - m[..., 2, 2],
                               1 - m[..., 0, 0] + m[..., 1, 1] - m[..., 2, 2],
                               1 - m[..., 0, 0] - m[..., 1, 1] + m[..., 2, 2]], -1), axis=-1)
    q = np.take_along_axis(candidates, best[..., None, None], axis=-2)[..., 0, :]
    return q / np.linalg.norm(q, axis=-1, keepdims=True)


def _quat_to_mat(q):
    import numpy as np

    w, x, y, z = (q[..., k] for k in range(4))
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)], -1),
        np.stack([2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)], -1),
        np.stack([2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)], -1),
    ], -2)


def _slerp(a, b, t: float):
    """Spherical interpolation of rotation matrices [..., 3, 3] (shortest path)."""
    import numpy as np

    qa, qb = _mat_to_quat(a), _mat_to_quat(b)
    dot = np.sum(qa * qb, axis=-1, keepdims=True)
    qb = np.where(dot < 0, -qb, qb)
    dot = np.clip(np.abs(dot), -1.0, 1.0)
    theta = np.arccos(dot)
    sin = np.sin(theta)
    near = sin < 1e-6
    wa = np.where(near, 1 - t, np.sin((1 - t) * theta) / np.where(near, 1, sin))
    wb = np.where(near, t, np.sin(t * theta) / np.where(near, 1, sin))
    q = wa * qa + wb * qb
    return _quat_to_mat(q / np.linalg.norm(q, axis=-1, keepdims=True))


def cut_loop(local_rot_mats, root_positions, start: int, end: int, blend: int):
    """Frames [start, end] as a seamless cycle: rotations [L+1, J, 3, 3] and root positions [L+1, 3].

    The last ``blend`` frames are faded into the frames that lead up to
    ``start`` -- moved rigidly (turned by the heading change, shifted by the
    travel) to where the cycle ends -- so the end flows into the start as it did
    in the original clip. The root joint's rotation is global (Kimodo's root has
    no parent), so only it and the root position take that rigid move.

    The clip closes with a copy of its first pose, moved the same way: game
    engines loop by wrapping time and expect the last key to equal the first
    (as Mixamo's loops do); without it Unity hitches once per cycle.
    """
    import numpy as np

    rots = np.array(local_rot_mats, dtype=np.float64, copy=True)
    root = np.array(root_positions, dtype=np.float64, copy=True)
    delta = _yaw(rots[end, 0]) - _yaw(rots[start, 0])
    turn = _rot_y(np.asarray(delta))
    flat = np.array([1.0, 0.0, 1.0])
    for k in range(end - blend, end):
        source = start - (end - k)
        weight = (k - (end - blend) + 1) / (blend + 1)
        weight = weight * weight * (3 - 2 * weight)           # smoothstep
        moved = rots[source].copy()
        moved[0] = turn @ moved[0]
        moved_root = turn @ ((root[source] - root[start]) * flat) + root[end] * flat
        moved_root[1] = root[source, 1]
        rots[k] = _slerp(rots[k], moved, weight)
        root[k] = (1 - weight) * root[k] + weight * moved_root
    closing_rots = rots[start].copy()
    closing_rots[0] = turn @ closing_rots[0]
    closing_root = root[end] * flat + root[start] * np.array([0.0, 1.0, 0.0])
    out_rots = np.concatenate([rots[start:end], closing_rots[None]], axis=0)
    out_root = np.concatenate([root[start:end], closing_root[None]], axis=0)
    return out_rots, out_root - root[start] * flat                # the cycle starts where the clip did


# ---------------------------------------------------------------------------
# Constraints (ANIM-006)
# ---------------------------------------------------------------------------

CONSTRAINT_TYPES = ("root2d", "fullbody", "end-effector", "left-hand", "right-hand", "left-foot", "right-foot")


def path_constraint(path: list, total_frames: int) -> dict:
    """Waypoints ``[[x, z], ...]`` (metres; the clip starts at 0,0 facing +z, +x is the character's left)
    as Kimodo's root2d constraint, reached at a constant speed over the whole clip."""
    import math

    points = [(0.0, 0.0)] + [(float(x), float(z)) for x, z in path]
    lengths = [math.dist(points[k], points[k + 1]) for k in range(len(points) - 1)]
    total = sum(lengths)
    if total <= 0:
        raise ValueError("the path does not go anywhere")
    frames, walked = [0], 0.0
    for length in lengths:
        walked += length
        frames.append(round((total_frames - 1) * walked / total))
    keep = [k for k in range(len(frames)) if k == 0 or frames[k] > frames[k - 1]]
    return {"type": "root2d", "frame_indices": [frames[k] for k in keep],
            "smooth_root_2d": [list(points[k]) for k in keep]}


# ---------------------------------------------------------------------------
# Preview (ANIM-005): a stick-figure contact sheet an agent can look at
# ---------------------------------------------------------------------------

PREVIEW_FRAMES = 8
_CELL = 220
_PAD = 14


def render_preview(motion: dict, parents: list, names: list, out_path: str, title: str = "") -> str:
    """Front and side views of evenly spaced frames, plus the top-down root path, as one PNG."""
    import numpy as np
    from PIL import Image, ImageDraw

    joints = np.asarray(motion["posed_joints"], dtype=np.float64)
    root = np.asarray(motion["root_positions"], dtype=np.float64)
    frames = joints.shape[0]
    fps = float(motion.get("fps", 30.0))
    picks = np.linspace(0, frames - 1, min(PREVIEW_FRAMES, frames)).round().astype(int)
    local = joints.copy()
    local[:, :, 0] -= root[:, None, 0]
    local[:, :, 2] -= root[:, None, 2]
    span = max(float(np.ptp(local[..., 1])), float(np.ptp(local[..., 0])), float(np.ptp(local[..., 2])), 0.5)
    scale = (_CELL - 2 * _PAD - 12) / span
    floor = local[..., 1].min()
    top = 18
    sheet = Image.new("RGB", (_CELL * (len(picks) + 1), _CELL * 2 + top), "white")
    draw = ImageDraw.Draw(sheet)
    draw.text((4, 3), title[:180], fill="black")
    left = [j for j, n in enumerate(names) if n.startswith("Left")]
    for col, frame in enumerate(picks):
        for row, axis in enumerate((0, 2)):            # front view: x across; side view: z across
            ox, oy = col * _CELL, top + row * _CELL

            def point(j, frame=frame, axis=axis, ox=ox, oy=oy):
                return (local[frame, j, axis] * scale + ox + _CELL / 2,
                        oy + _CELL - _PAD - (local[frame, j, 1] - floor) * scale)

            draw.line([(ox + 4, oy + _CELL - _PAD), (ox + _CELL - 4, oy + _CELL - _PAD)], fill=(200, 200, 200))
            for j, parent in enumerate(parents):
                if parent >= 0:
                    colour = (200, 40, 40) if j in left else (40, 40, 200)
                    draw.line([point(parent), point(j)], fill=colour, width=2)
            draw.text((ox + 4, oy + 3), f"{'front' if axis == 0 else 'side'} {frame / fps:.2f}s", fill="black")
    ox = len(picks) * _CELL
    path = root[:, [0, 2]]
    pspan = max(float(np.ptp(path[:, 0])), float(np.ptp(path[:, 1])), 0.5)
    pscale = (2 * _CELL - 2 * _PAD) / pspan * 0.5
    centre = (path.max(0) + path.min(0)) / 2
    trail = [(ox + _CELL / 2 - (x - centre[0]) * pscale, top + _CELL - (z - centre[1]) * pscale) for x, z in path]
    if len(trail) > 1:
        draw.line(trail, fill=(0, 140, 0), width=2)
    draw.ellipse([trail[0][0] - 4, trail[0][1] - 4, trail[0][0] + 4, trail[0][1] + 4], fill=(0, 140, 0))
    draw.text((ox + 4, top + 3), f"root path, top view ({pspan:.2f} m)", fill="black")
    draw.text((ox + 4, top + 15), "dot = start, up = +z", fill="black")
    sheet.save(out_path)
    return out_path


# ---------------------------------------------------------------------------
# Cached text encoder (passed to kimodo.load_model(text_encoder=...))
# ---------------------------------------------------------------------------

class CachedTextEncoder:
    """Kimodo's text-encoder interface (``feats, lengths = encoder(texts)``) with a disk cache.

    Returns exactly what ``LLM2VecEncoder`` returns -- float32 ``[B, 1, 4096]``
    on the CPU and lengths of 1 -- so a cache hit is indistinguishable from a
    fresh encode (LLM2Vec runs with batch size 1, which keeps it deterministic).
    """

    def __init__(self, cache_dir: str, encoder_id: str) -> None:
        self.cache_dir = cache_dir
        self.encoder_id = encoder_id
        self._encoder = None
        self.hits = 0
        self.misses = 0
        self.load_seconds: float | None = None
        self.encode_seconds = 0.0

    @property
    def loaded(self) -> bool:
        return self._encoder is not None

    def _load(self):
        if self._encoder is None:
            log("loading the text encoder (Llama-3-8B via LLM2Vec, bf16 on the CPU, ~15 GB of RAM)...")
            started = time.monotonic()
            from kimodo.model.llm2vec import LLM2VecEncoder
            from kimodo.model.load_model import TEXT_ENCODER_PRESETS

            kwargs = dict(TEXT_ENCODER_PRESETS["llm2vec"]["kwargs"])
            kwargs["device"] = "cpu"
            self._encoder = LLM2VecEncoder(**kwargs)
            self.load_seconds = time.monotonic() - started
            log(f"text encoder loaded in {self.load_seconds:.0f}s")
        return self._encoder

    def __call__(self, texts):
        import numpy as np
        import torch

        single = isinstance(texts, str)
        texts = [texts] if single else list(texts)
        vectors: list = [None] * len(texts)
        missing: list[int] = []
        for index, text in enumerate(texts):
            if not text.strip():
                vectors[index] = np.zeros(EMBEDDING_DIM, dtype=np.float32)
                continue
            path = embedding_path(self.cache_dir, self.encoder_id, text)
            if os.path.isfile(path):
                vectors[index] = np.load(path)
                self.hits += 1
            else:
                missing.append(index)
        if missing:
            encoder = self._load()
            started = time.monotonic()
            feats, _lengths = encoder([texts[i] for i in missing])
            fresh = feats[:, 0].detach().to(torch.float32).cpu().numpy()
            self.encode_seconds += time.monotonic() - started
            for row, index in enumerate(missing):
                vectors[index] = fresh[row]
                _save_atomic(embedding_path(self.cache_dir, self.encoder_id, texts[index]), fresh[row])
            self.misses += len(missing)
            log(f"encoded {len(missing)} new prompt(s) in {time.monotonic() - started:.1f}s")
        stacked = torch.from_numpy(np.stack(vectors).astype(np.float32))[:, None]
        lengths = [1] * len(texts)
        if single:
            return stacked[0], lengths[0]
        return stacked, lengths

    # Kimodo's own encoder exposes these; nothing in generation needs them to do more.
    def to(self, *_args, **_kwargs):
        return self

    def eval(self):
        return self

    def get_device(self):
        import torch
        return torch.device("cpu")


def _save_atomic(path: str, vector) -> None:
    import numpy as np

    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "wb") as handle:
        np.save(handle, vector.astype(np.float32))
    os.replace(temp, path)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, model_name: str, cache_dir: str, encoder_id: str) -> None:
        import torch
        from kimodo import load_model
        from kimodo.skeleton import SOMASkeleton30

        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        if self.device == "cpu":
            log("WARNING: CUDA is not available; generating on the CPU will be very slow")
        self.encoder = CachedTextEncoder(cache_dir, encoder_id)
        started = time.monotonic()
        self.model, self.resolved = load_model(model_name, device=self.device, default_family="Kimodo",
                                               return_resolved_name=True, text_encoder=self.encoder)
        skeleton = self.model.skeleton
        if isinstance(skeleton, SOMASkeleton30):
            skeleton = skeleton.somaskel77.to(self.device)
        self.skeleton = skeleton
        log(f"{self.resolved} loaded on {self.device} in {time.monotonic() - started:.0f}s")

    def health(self) -> dict:
        return {"ok": True, "model": self.resolved, "device": self.device, "encoderLoaded": self.encoder.loaded,
                "fps": float(self.model.fps)}

    def generate(self, request: dict) -> dict:
        import torch
        from kimodo.constraints import load_constraints_lst
        from kimodo.exports.bvh import save_motion_bvh
        from kimodo.exports.motion_io import save_kimodo_npz
        from kimodo.skeleton import global_rots_to_local_rots
        from kimodo.tools import seed_everything

        segments = request["segments"]
        texts = [str(s["text"]) for s in segments]
        fps = float(self.model.fps)
        loop = request.get("loop")
        if loop:
            # Generate longer than the cycle, so a well-matched cycle can be cut from inside it.
            frames = [max(1, int(loop_generation_seconds(float(loop["seconds"])) * fps))]
        else:
            frames = [max(1, int(float(s["seconds"]) * fps)) for s in segments]
        constraint_dicts = list(request.get("constraints") or [])
        if request.get("path"):
            constraint_dicts.append(path_constraint(request["path"], sum(frames)))
        constraints = load_constraints_lst(constraint_dicts, self.model.skeleton) if constraint_dicts else []
        seed = int(request["seed"])
        hits, misses, encode_before = self.encoder.hits, self.encoder.misses, self.encoder.encode_seconds
        load_before = self.encoder.load_seconds
        started = time.monotonic()
        seed_everything(seed)
        output = self.model(
            texts, frames, constraint_lst=constraints, num_denoising_steps=int(request["steps"]), num_samples=1,
            multi_prompt=True, num_transition_frames=int(request["transitionFrames"]),
            post_processing=bool(request["postprocess"]), return_numpy=True,
        )
        motion = {k: (v[0] if hasattr(v, "shape") and len(v.shape) > 0 and v.shape[0] == 1 else v)
                  for k, v in output.items()}
        loop_info = None
        if loop:
            motion, loop_info = self._loop(motion, fps, round(float(loop["seconds"]) * fps))
        stats = motion_stats(motion, fps)
        if request.get("inPlace"):
            motion = make_in_place(motion)
        save_kimodo_npz(request["npz"], motion)
        joints = torch.from_numpy(motion["posed_joints"]).to(self.device)
        rotations = torch.from_numpy(motion["global_rot_mats"]).to(self.device)
        local = global_rots_to_local_rots(rotations, self.skeleton)
        save_motion_bvh(request["bvh"], local, joints[:, self.skeleton.root_idx, :], skeleton=self.skeleton,
                        fps=fps, standard_tpose=bool(request.get("standardTpose", True)))
        preview = None
        if request.get("preview"):
            parents = [int(p) for p in self.skeleton.joint_parents]
            preview = render_preview({**motion, "fps": fps}, parents, list(self.skeleton.bone_order_names),
                                     request["preview"], title=request.get("title", ""))
        seconds = time.monotonic() - started
        encoder_load = None
        if self.encoder.load_seconds is not None and load_before is None:
            encoder_load = round(self.encoder.load_seconds, 1)
        log(f"{stats['frames']} frames (seed {seed}) in {seconds:.0f}s")
        return {
            "stats": stats,
            "loop": loop_info,
            "constraints": len(constraints),
            "preview": preview,
            "seconds": round(seconds, 1),
            "prompts": {"cached": self.encoder.hits - hits, "encoded": self.encoder.misses - misses},
            "encodeSeconds": round(self.encoder.encode_seconds - encode_before, 1),
            "encoderLoadSeconds": encoder_load,
            "model": self.resolved,
            "skeleton": {"name": self.skeleton.name, "joints": int(self.skeleton.nbjoints)},
        }

    def _loop(self, motion: dict, fps: float, target_frames: int) -> tuple[dict, dict]:
        """Cut the best cycle out of ``motion`` and rebuild Kimodo's full motion dict for it."""
        import numpy as np
        import torch
        from kimodo.exports.motion_io import complete_motion_dict

        found = find_loop(motion, fps, target_frames)
        rots, root = cut_loop(motion["local_rot_mats"], motion["root_positions"],
                              found["start"], found["end"], found["blend"])
        rebuilt = complete_motion_dict(torch.from_numpy(rots).float().to(self.device),
                                       torch.from_numpy(root).float().to(self.device), self.skeleton, fps)
        looped = {k: v.detach().cpu().numpy() if hasattr(v, "detach") else v for k, v in rebuilt.items()}
        # How smooth the wrap is: the step into the closing frame (= the first pose, moved),
        # against a normal frame-to-frame step.
        pose, _velocity = _loop_features(np.asarray(looped["local_rot_mats"], dtype=np.float64),
                                         np.asarray(looped["root_positions"], dtype=np.float64),
                                         np.asarray(looped["posed_joints"], dtype=np.float64), fps)
        steps = np.linalg.norm(np.diff(pose, axis=0), axis=-1).mean(-1)
        seam = float(steps[-1])
        info = {**found, "cycleFrames": int(found["end"] - found["start"]),
                "cycleSeconds": round((found["end"] - found["start"]) / fps, 3),
                "generatedFrames": int(np.asarray(motion["posed_joints"]).shape[0]),
                "seamStepM": round(seam, 4), "typicalStepM": round(float(np.median(steps)), 4)}
        log(f"loop: frames {found['start']}-{found['end']} ({info['cycleSeconds']}s), "
            f"seam step {seam * 100:.1f} cm vs typical {info['typicalStepM'] * 100:.1f} cm")
        return looped, info


def serve(args: argparse.Namespace) -> int:
    engine = Engine(args.model, args.cache_dir, args.encoder_id)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # the request log is noise; our own lines say what ran
            return

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, engine.health())
            else:
                self._reply(404, {"error": f"unknown path {self.path}"})

        def do_POST(self) -> None:
            if self.path != "/generate":
                self._reply(404, {"error": f"unknown path {self.path}"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length) or b"{}")
                if not request.get("segments") or not request.get("bvh") or not request.get("npz"):
                    self._reply(400, {"error": "segments, bvh and npz are required"})
                    return
            except ValueError as exc:
                self._reply(400, {"error": f"bad request: {exc}"})
                return
            try:
                self._reply(200, engine.generate(request))
            except Exception as exc:  # noqa: BLE001 - report every failure to the caller, keep serving
                traceback.print_exc()
                sys.stdout.flush()
                self._reply(500, {"error": f"{type(exc).__name__}: {exc}"})

    server = HTTPServer((args.host, args.port), Handler)
    log(f"listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Install-time fetching and import checks live in backend/engines/hf_fetch.py (shared by every Python engine).
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--model", required=True)
    p_serve.add_argument("--cache-dir", required=True)
    p_serve.add_argument("--encoder-id", required=True)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
