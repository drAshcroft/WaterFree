"""animgen: clip parsing, the embedding-cache contract, the RAM gate, and make() against a stub worker.

No GPU, no Kimodo venv, no Blender: the worker process is replaced by a stub
that writes placeholder files and emulates the embedding cache, and the HF
cache is redirected to a temp folder.
"""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest import mock

import numpy as np

from backend.animgen import engine as kimodo
from backend.animgen import fbx as fbx_lib
from backend.animgen import generate as anim
from backend.animgen import kimodo_worker as worker
from backend.animgen.config import AnimgenConfig
from backend.cli.dispatcher import dispatch
from backend.engines import pyenv
from backend.engines.install import InstallError
from backend.gpu import ram
from backend.test_support import make_temp_dir

MARKER = {
    "version": "v1.2",
    "revisions": {repo.repo: repo.revision for repo in kimodo.KIMODO.repos},
}
PLENTY = ram.SystemMemory(total_gb=32.0, available_gb=30.0)
SHORT = ram.SystemMemory(total_gb=32.0, available_gb=8.0)


class StubWorker:
    """Stands in for EngineServer running kimodo_worker.py serve."""

    instances: ClassVar[list["StubWorker"]] = []

    def __init__(self, *, name, argv, cwd=None, env=None, report=None, echo_log=False, **_):
        self.name, self.argv, self.env = name, argv, env or {}
        self.cache_dir = argv[argv.index("--cache-dir") + 1]
        self.encoder_id = argv[argv.index("--encoder-id") + 1]
        self.requests: list[dict] = []
        self.load_seconds = 2.0
        self.log_path = None
        StubWorker.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def running(self):
        return True

    def log_tail(self):
        return ""

    def spill_report(self):
        return {"vramSpillMb": 0, "sharedPeakMb": 70, "dedicatedPeakMb": 1300, "spilled": False}

    def post_json(self, path, payload, *, timeout):
        assert path == "/generate"
        self.requests.append(payload)
        Path(payload["bvh"]).write_text("HIERARCHY\n", encoding="utf-8")
        Path(payload["npz"]).write_bytes(b"npz")
        if payload.get("preview"):
            Path(payload["preview"]).write_bytes(b"png")
        encoded = 0
        for segment in payload["segments"]:
            target = worker.embedding_path(self.cache_dir, self.encoder_id, segment["text"])
            if not os.path.isfile(target):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                np.save(target, np.zeros(worker.EMBEDDING_DIM, dtype=np.float32))
                encoded += 1
        answer = {
            "stats": {"frames": 120, "fps": 30.0, "seconds": 4.0, "rootTravelM": 6.1,
                      "rootDisplacementM": 6.0, "loopGapM": 0.21},
            "seconds": 3.0,
            "prompts": {"cached": len(payload["segments"]) - encoded, "encoded": encoded},
            "encodeSeconds": 0.5 if encoded else 0.0,
            "encoderLoadSeconds": 95.0 if encoded else None,
            "model": "kimodo-soma-rp-v1.2",
            "skeleton": {"name": "somaskel77", "joints": 77},
            "preview": payload.get("preview"),
            "constraints": len(payload.get("constraints") or []) + (1 if payload.get("path") else 0),
            "loop": {"start": 20, "end": 56, "blend": 8, "poseErrorM": 0.02, "velocityError": 0.1,
                     "cycleFrames": 36, "cycleSeconds": 1.2, "generatedFrames": 120, "seamStepM": 0.03,
                     "typicalStepM": 0.03} if payload.get("loop") else None,
        }
        return json.dumps(answer).encode(), {}


class AnimgenTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = make_temp_dir(self, prefix="animgen-")
        self.hf_cache = make_temp_dir(self, prefix="animgen-hf-")
        self.embeddings = make_temp_dir(self, prefix="animgen-emb-")
        StubWorker.instances = []
        self.messages: list[str] = []
        self.memory = PLENTY
        patches = [
            mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(self.hf_cache)}),
            mock.patch("backend.animgen.engine.embedding_cache_dir", return_value=self.embeddings),
            mock.patch("backend.animgen.generate.EngineServer", StubWorker),
            mock.patch("backend.animgen.generate.kimodo.require", return_value=MARKER),
            mock.patch("backend.animgen.generate.fbx_lib.find_blender", return_value=None),
            mock.patch("backend.gpu.ram.query", side_effect=lambda: self.memory),
            mock.patch("backend.gpu.ram.largest_programs",
                       return_value=[{"name": "chrome.exe", "gb": 8.1, "processes": 58}]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make(self, clips, **kw):
        kw.setdefault("config", AnimgenConfig(seed=5))
        kw.setdefault("report", self.messages.append)
        return anim.make(clips, workspace=str(self.workspace), **kw)


class MakeTests(AnimgenTestCase):
    def test_make_writes_bvh_npz_and_sidecar_with_provenance(self) -> None:
        result = self.make(["a person jogs forward"])
        self.assertEqual(result["count"], 1)
        entry = result["files"][0]
        self.assertTrue(Path(entry["bvh"]).is_file() and Path(entry["npz"]).is_file())
        self.assertIn(str(self.workspace / ".waterfree" / "generated" / "animations"), entry["bvh"])
        sidecar = json.loads(Path(entry["metadata"]).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["seed"], 5)
        self.assertEqual(sidecar["engine"]["commit"], kimodo.KIMODO.commit)
        self.assertEqual(sidecar["model"]["revision"], MARKER["revisions"]["nvidia/Kimodo-SOMA-RP-v1.2"])
        self.assertIn("Meta Llama 3", sidecar["licence"])
        self.assertEqual(sidecar["skeleton"]["joints"], 77)
        self.assertEqual(sidecar["textEncoder"]["device"], "cpu")
        self.assertIn("Blender", result["fbx"]["skipped"])
        self.assertFalse(result["vramSpill"]["spilled"], "the worker's spill watchdog report is passed through")

    def test_worker_runs_offline_with_the_encoder_on_the_cpu(self) -> None:
        self.make(["a person waves"])
        stub = StubWorker.instances[0]
        self.assertEqual(stub.env["TEXT_ENCODER_DEVICE"], "cpu")
        self.assertEqual(stub.env["HF_HUB_OFFLINE"], "1")
        self.assertEqual(stub.argv[stub.argv.index("--model") + 1], "kimodo-soma-rp-v1.2")
        self.assertIn("-I", stub.argv, "the worker must not import from WaterFree's venv or cwd")

    def test_request_follows_the_config(self) -> None:
        config = AnimgenConfig(seed=1, seconds=2.5, steps=60, in_place=True, tpose="seed", postprocess=False,
                               transition_frames=8)
        self.make(["a person jumps in place"], config=config)
        request = StubWorker.instances[0].requests[0]
        self.assertEqual(request["segments"], [{"text": "a person jumps in place", "seconds": 2.5}])
        self.assertEqual((request["steps"], request["transitionFrames"]), (60, 8))
        self.assertTrue(request["inPlace"])
        self.assertFalse(request["standardTpose"])
        self.assertFalse(request["postprocess"])

    def test_variants_get_consecutive_seeds_and_numbered_names(self) -> None:
        result = self.make([{"name": "orc_idle", "text": "a heavy creature stands, breathing", "seed": 10}],
                           count=2)
        self.assertEqual([f["seed"] for f in result["files"]], [10, 11])
        self.assertEqual([Path(f["bvh"]).name for f in result["files"]], ["orc_idle_01.bvh", "orc_idle_02.bvh"])
        self.assertEqual(len(StubWorker.instances), 1, "one worker serves the whole batch")

    def test_a_sequence_is_one_clip_of_several_segments(self) -> None:
        clip = {"name": "draw_swing", "segments": [{"text": "a person draws a sword", "seconds": 1.5},
                                                   {"text": "a person swings the sword", "seconds": 2}]}
        result = self.make([clip])
        request = StubWorker.instances[0].requests[0]
        self.assertEqual([s["seconds"] for s in request["segments"]], [1.5, 2.0])
        self.assertEqual(result["files"][0]["prompt"], "a person draws a sword / a person swings the sword")

    def test_bad_clips_fail_before_the_gpu_is_touched(self) -> None:
        for clips in (["   "], [{"text": "walk", "seconds": 12}],
                      [{"segments": [{"text": "walk", "seconds": 10}] * 7}], [{"segments": []}]):
            with self.assertRaises(ValueError):
                self.make(clips)
        with self.assertRaises(ValueError):
            self.make(["walk"], count=9)
        self.assertEqual(StubWorker.instances, [])

    def test_fbx_always_without_blender_fails_before_the_gpu_is_touched(self) -> None:
        with self.assertRaises(InstallError):
            self.make(["walk"], config=AnimgenConfig(fbx="always"))
        self.assertEqual(StubWorker.instances, [])

    def test_fbx_conversion_is_recorded_in_the_result_and_sidecar(self) -> None:
        def fake_convert(jobs, blender):
            for job in jobs:
                Path(job["fbx"]).write_bytes(b"fbx")
            return [{**job, "ok": True, "fps": 30, "bones": 78} for job in jobs]

        with mock.patch("backend.animgen.generate.fbx_lib.find_blender", return_value=Path("blender.exe")), \
                mock.patch("backend.animgen.generate.fbx_lib.convert", side_effect=fake_convert):
            result = self.make(["a person waves"])
        entry = result["files"][0]
        self.assertTrue(entry["fbx"].endswith(".fbx"))
        self.assertEqual(result["fbx"]["converted"], 1)
        sidecar = json.loads(Path(entry["metadata"]).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["files"]["fbx"], entry["fbx"])
        self.assertEqual(sidecar["fbx"]["fps"], 30)


class LoopPathConstraintTests(AnimgenTestCase):
    """ANIM-003 (loops), ANIM-005 (previews), ANIM-006 (constraints and paths) on the WaterFree side."""

    def test_a_loop_asks_the_worker_for_a_cycle_and_reports_the_seam(self) -> None:
        result = self.make(["a person jogs"], config=AnimgenConfig(seed=1, seconds=1.5, loop=True, in_place=True))
        request = StubWorker.instances[0].requests[0]
        self.assertEqual(request["loop"], {"seconds": 1.5})
        self.assertTrue(request["inPlace"])
        entry = result["files"][0]
        self.assertEqual(entry["loop"]["cycleSeconds"], 1.2)
        sidecar = json.loads(Path(entry["metadata"]).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["loop"]["cycleFrames"], 36)
        self.assertTrue(sidecar["settings"]["loop"])

    def test_loops_that_cannot_work_are_refused_before_the_gpu(self) -> None:
        loop = AnimgenConfig(loop=True)
        for clip in ({"segments": ["a person walks", "a person runs"]},
                     {"text": "a person walks", "seconds": 7},
                     {"text": "a person walks", "path": [[0, 3]]}):
            with self.assertRaises(ValueError):
                self.make([clip], config=loop)
        self.assertEqual(StubWorker.instances, [])

    def test_previews_are_requested_and_recorded_unless_turned_off(self) -> None:
        result = self.make(["a person waves"])
        request = StubWorker.instances[0].requests[0]
        self.assertTrue(request["preview"].endswith(".png"))
        self.assertIn("a person waves", request["title"])
        self.assertTrue(Path(result["files"][0]["preview"]).is_file())
        self.make(["a person waves"], config=AnimgenConfig(preview=False))
        self.assertNotIn("preview", StubWorker.instances[1].requests[0])

    def test_path_and_constraints_file_reach_the_worker(self) -> None:
        folder = self.workspace / "batch"
        folder.mkdir()
        (folder / "pose.json").write_text(json.dumps(
            [{"type": "left-hand", "frame_indices": [30], "local_joints_rot": [], "root_positions": []}]),
            encoding="utf-8")
        batch = folder / "clips.json"
        batch.write_text(json.dumps([
            {"name": "patrol", "text": "a person walks", "path": [[0, 3], [2, 5]]},
            {"name": "reach", "text": "a person reaches up", "constraints": "pose.json"},
        ]), encoding="utf-8")
        result = self.make(anim.parse_clips_file(str(batch)))
        patrol, reach = StubWorker.instances[0].requests
        self.assertEqual(patrol["path"], [[0.0, 3.0], [2.0, 5.0]])
        self.assertEqual(reach["constraints"][0]["type"], "left-hand", "relative to the batch file, not the cwd")
        sidecar = json.loads(Path(result["files"][0]["metadata"]).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["constraints"]["path"], [[0.0, 3.0], [2.0, 5.0]])

    def test_bad_paths_and_constraints_are_refused(self) -> None:
        for clip in ({"text": "walk", "path": [[0, 0]]}, {"text": "walk", "path": [["a", 1]]},
                     {"text": "walk", "path": [[500, 0]]}, {"text": "walk", "constraints": [{"type": "teleport"}]},
                     {"text": "walk", "constraints": [{"type": "root2d"}]}):
            with self.assertRaises(ValueError):
                self.make([clip])
        self.assertEqual(StubWorker.instances, [])

    def test_cli_path_flag(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch(["animgen", "make", "a person walks", "--path", "0,3", "2,5", "--fbx", "never",
                             "--workspace", str(self.workspace)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(StubWorker.instances[0].requests[0]["path"], [[0.0, 3.0], [2.0, 5.0]])
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = dispatch(["animgen", "make", "a person walks", "--path", "north", "--workspace",
                             str(self.workspace)])
        self.assertEqual(code, 2)


class RamGateTests(AnimgenTestCase):
    """ANIM-002: refuse to load the 15 GB CPU encoder into a machine without the RAM for it."""

    def test_new_prompts_with_short_ram_are_refused_before_the_worker_starts(self) -> None:
        self.memory = SHORT
        with self.assertRaises(ram.InsufficientRam) as caught:
            self.make(["a person does a cartwheel"])
        self.assertEqual(StubWorker.instances, [])
        body = caught.exception.to_json()
        self.assertEqual(body["largestPrograms"][0]["name"], "chrome.exe")
        self.assertEqual(body["needGb"], 17.0)
        self.assertIn("--skip-ram-check", str(caught.exception))

    def test_cached_prompts_never_need_the_encoder_or_the_ram(self) -> None:
        self.make(["a person does a cartwheel"])             # encodes and caches it
        self.memory = SHORT
        self.messages.clear()
        result = self.make(["a person does a cartwheel"], config=AnimgenConfig(seed=99))
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["textEncoder"]["newPrompts"], 0)
        self.assertTrue(any("will not load" in m for m in self.messages))

    def test_skip_ram_check_runs_anyway(self) -> None:
        self.memory = SHORT
        result = self.make(["a person crawls"], check_ram=False)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["textEncoder"]["encoded"], 1)

    def test_the_cli_reports_short_ram_as_exit_4_with_json(self) -> None:
        self.memory = SHORT
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch(["animgen", "make", "a person somersaults", "--workspace", str(self.workspace),
                             "--fbx", "never"])
        self.assertEqual(code, 4)
        self.assertTrue(json.loads(out.getvalue())["insufficientRam"])


class ClipsFileTests(AnimgenTestCase):
    def test_txt_and_json_formats(self) -> None:
        txt = self.workspace / "clips.txt"
        txt.write_text("# locomotion\na person walks\n\na person runs\n", encoding="utf-8")
        self.assertEqual(anim.parse_clips_file(str(txt)), [{"text": "a person walks"}, {"text": "a person runs"}])
        js = self.workspace / "clips.json"
        js.write_text(json.dumps(["wave", {"name": "Act 2: fall!", "text": "a person falls", "seed": 3}]),
                      encoding="utf-8")
        clips = anim.parse_clips_file(str(js))
        normalised = anim.normalise_clip(clips[1], AnimgenConfig(), 2)
        self.assertEqual(normalised["name"], "Act-2-fall")
        self.assertEqual(normalised["seed"], 3)
        self.assertEqual(normalised["segments"], [{"text": "a person falls", "seconds": 4.0}])

    def test_duplicate_names_are_refused(self) -> None:
        js = self.workspace / "dupes.json"
        js.write_text(json.dumps([{"name": "idle", "text": "a"}, {"name": "idle", "text": "b"}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            anim.parse_clips_file(str(js))


class WorkerContractTests(unittest.TestCase):
    def test_cache_keys_depend_on_text_and_encoder(self) -> None:
        a = worker.embedding_path("c", "enc1", "a person walks")
        self.assertEqual(a, worker.embedding_path("c", "enc1", "a person walks"))
        self.assertNotEqual(a, worker.embedding_path("c", "enc2", "a person walks"))
        self.assertNotEqual(a, worker.embedding_path("c", "enc1", "a person runs"))
        self.assertTrue(worker.is_cached("c", "enc1", "  "), "Kimodo zeroes empty prompts; no encoder needed")

    def test_encoder_id_names_every_text_encoder_revision(self) -> None:
        identity = kimodo.encoder_id(MARKER)
        for repo in ("meta-llama/Meta-Llama-3-8B-Instruct", "McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp"):
            self.assertIn(f"{repo}@{MARKER['revisions'][repo]}", identity)

    def test_cached_encoder_serves_hits_without_loading_llama(self) -> None:
        import torch

        cache = make_temp_dir(self, prefix="emb-")
        vector = np.arange(worker.EMBEDDING_DIM, dtype=np.float32)
        target = worker.embedding_path(str(cache), "enc", "a person walks")
        os.makedirs(os.path.dirname(target))
        np.save(target, vector)
        encoder = worker.CachedTextEncoder(str(cache), "enc")
        with mock.patch.object(encoder, "_load", side_effect=AssertionError("must not load the encoder")):
            feats, lengths = encoder(["a person walks", ""])
        self.assertEqual(tuple(feats.shape), (2, 1, worker.EMBEDDING_DIM))
        self.assertEqual(feats.dtype, torch.float32)
        self.assertTrue(torch.equal(feats[0, 0], torch.from_numpy(vector)))
        self.assertEqual(float(feats[1].abs().sum()), 0.0)
        self.assertEqual(lengths, [1, 1])
        self.assertFalse(encoder.loaded)

    def test_in_place_removes_travel_but_keeps_hip_sway(self) -> None:
        frames = 10
        travel = np.linspace(0.0, 5.0, frames, dtype=np.float32)
        sway = np.array([0.05, -0.05] * 5, dtype=np.float32)
        smooth = np.stack([travel, np.full(frames, 0.9), np.zeros(frames)], axis=1).astype(np.float32)
        root = smooth.copy()
        root[:, 0] += sway
        joints = np.repeat(root[:, None, :], 3, axis=1)
        joints[:, 1, 1] += 0.5
        motion = {"posed_joints": joints, "root_positions": root, "smooth_root_pos": smooth}
        original_root = root.copy()
        stats = worker.motion_stats(motion, 30.0)
        self.assertAlmostEqual(stats["rootDisplacementM"], 4.9, places=2)   # 5 m of travel, sway +0.05 -> -0.05
        flat = worker.make_in_place(motion)
        np.testing.assert_allclose(flat["smooth_root_pos"][:, [0, 2]], 0.0, atol=1e-6)
        np.testing.assert_allclose(flat["root_positions"][:, 0], sway, atol=1e-6)
        np.testing.assert_allclose(flat["posed_joints"][:, 1, 1], joints[:, 1, 1], atol=1e-6)
        self.assertLess(worker.motion_stats(flat, 30.0)["rootDisplacementM"], 0.11)
        np.testing.assert_array_equal(motion["root_positions"], original_root, err_msg="the input is not modified")


def periodic_motion(frames: int = 150, period: int = 36, turn_per_frame: float = 0.0, speed: float = 0.04):
    """A 3-joint 'walker' that repeats every ``period`` frames, optionally turning at a constant rate."""
    t = np.arange(frames, dtype=np.float64)
    yaw = turn_per_frame * t
    rots = np.tile(np.eye(3), (frames, 3, 1, 1))
    rots[:, 0] = worker._rot_y(yaw)
    if turn_per_frame:
        radius = speed / turn_per_frame
        root = np.stack([radius * (1 - np.cos(yaw)), np.full(frames, 0.9), radius * np.sin(yaw)], axis=1)
    else:
        root = np.stack([np.zeros(frames), np.full(frames, 0.9), speed * t], axis=1)
    swing = 0.3 * np.sin(2 * np.pi * t / period)
    offsets = np.stack([np.zeros((frames, 3)), np.stack([0.1 + 0 * t, -0.5 + 0 * t, swing], 1),
                        np.stack([0 * t, 0.6 + 0 * t, 0.05 * swing], 1)], axis=1)
    joints = root[:, None, :] + np.einsum("tab,tjb->tja", worker._rot_y(yaw), offsets)
    return {"local_rot_mats": rots, "root_positions": root, "posed_joints": joints}


class LoopMathTests(unittest.TestCase):
    def test_quaternion_round_trip_and_slerp(self) -> None:
        rng = np.random.default_rng(3)
        axes = rng.normal(size=(20, 3))
        angles = rng.uniform(-3, 3, size=20)
        mats = []
        for axis, angle in zip(axes, angles, strict=True):
            k = axis / np.linalg.norm(axis)
            cross = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
            mats.append(np.eye(3) + np.sin(angle) * cross + (1 - np.cos(angle)) * cross @ cross)
        mats = np.array(mats)
        np.testing.assert_allclose(worker._quat_to_mat(worker._mat_to_quat(mats)), mats, atol=1e-9)
        a, b = worker._rot_y(np.array(0.0)), worker._rot_y(np.array(np.pi / 2))
        np.testing.assert_allclose(worker._slerp(a, b, 0.0), a, atol=1e-9)
        np.testing.assert_allclose(worker._slerp(a, b, 1.0), b, atol=1e-9)
        np.testing.assert_allclose(worker._slerp(a, b, 0.5), worker._rot_y(np.array(np.pi / 4)), atol=1e-9)

    def test_find_loop_finds_the_period(self) -> None:
        found = worker.find_loop(periodic_motion(period=36), 30.0, target_frames=40)
        self.assertEqual(found["end"] - found["start"], 36)
        self.assertLess(found["poseErrorM"], 1e-6)

    def test_cut_loop_carries_travel_through_the_seam(self) -> None:
        motion = periodic_motion(period=36)
        rots, root = worker.cut_loop(motion["local_rot_mats"], motion["root_positions"], 20, 56, 8)
        self.assertEqual(rots.shape[0], 37, "36 frames of cycle plus a closing copy of the first pose")
        np.testing.assert_allclose(root[0, [0, 2]], 0.0, atol=1e-12)
        # A perfectly periodic walk needs no blending: the cycle is the original frames, re-based,
        # and the closing frame is the first pose one cycle further on.
        np.testing.assert_allclose(root[:, 2], 0.04 * np.arange(37), atol=1e-9)
        np.testing.assert_allclose(rots[-1], rots[0], atol=1e-12)

    def test_cut_loop_turns_the_lead_in_with_the_heading(self) -> None:
        """Walking a circle is periodic under the rigid move cut_loop applies, so blending must change nothing."""
        motion = periodic_motion(frames=120, period=30, turn_per_frame=0.02)
        rots, root = worker.cut_loop(motion["local_rot_mats"], motion["root_positions"], 20, 50, 8)
        np.testing.assert_allclose(rots, motion["local_rot_mats"][20:51], atol=1e-9)
        expected = motion["root_positions"][20:51] - motion["root_positions"][20] * np.array([1, 0, 1])
        np.testing.assert_allclose(root, expected, atol=1e-9)

    def test_loops_are_generated_longer_than_the_cycle(self) -> None:
        self.assertEqual(worker.loop_generation_seconds(2.0), 4.3)
        self.assertEqual(worker.loop_generation_seconds(6.0), 9.9)
        self.assertEqual(worker.loop_generation_seconds(9.0), 10.0)

    def test_path_becomes_a_constant_speed_root2d_constraint(self) -> None:
        constraint = worker.path_constraint([[0, 3], [3, 3]], total_frames=91)
        self.assertEqual(constraint["type"], "root2d")
        self.assertEqual(constraint["frame_indices"], [0, 45, 90])
        self.assertEqual(constraint["smooth_root_2d"], [[0.0, 0.0], [0.0, 3.0], [3.0, 3.0]])
        with self.assertRaises(ValueError):
            worker.path_constraint([[0, 0]], total_frames=30)

    def test_preview_renders_a_png(self) -> None:
        from PIL import Image

        folder = make_temp_dir(self, prefix="preview-")
        motion = periodic_motion(frames=40)
        out = worker.render_preview({**motion, "fps": 30.0}, [-1, 0, 0], ["Hips", "LeftUpLeg", "Head"],
                                    str(folder / "clip.png"), title="a person walks")
        with Image.open(out) as image:
            self.assertEqual(image.size, (220 * 9, 220 * 2 + 18))


class EngineStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hf_cache = make_temp_dir(self, prefix="animgen-hf-")
        patcher = mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(self.hf_cache)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_snapshot(self, repo: str, revision: str, files: tuple, main: str | None = None) -> None:
        folder = self.hf_cache / ("models--" + repo.replace("/", "--"))
        (folder / "refs").mkdir(parents=True, exist_ok=True)
        (folder / "refs" / "main").write_text(main or revision, encoding="utf-8")
        for name in files:
            path = folder / "snapshots" / revision / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    def test_a_complete_snapshot_is_present(self) -> None:
        entry = kimodo.KIMODO.repo("McGill-NLP/LLM2Vec-Meta-Llama-3-8B-Instruct-mntp-supervised")
        self.fake_snapshot(entry.repo, "abc", entry.required)
        self.assertTrue(pyenv.repo_status(entry, "abc")["present"])

    def test_a_missing_file_or_a_moved_ref_is_not_present(self) -> None:
        entry = kimodo.KIMODO.repo("nvidia/Kimodo-SOMA-RP-v1.2")
        self.fake_snapshot(entry.repo, "abc", ("config.yaml",))
        self.assertEqual(pyenv.repo_status(entry, "abc")["missing"], ["model.safetensors"])
        self.fake_snapshot(entry.repo, "abc", entry.required, main="def")
        moved = pyenv.repo_status(entry, "abc")
        self.assertTrue(moved["movedSinceInstall"])
        self.assertFalse(moved["present"])

    def test_status_and_make_without_install_say_how_to_install(self) -> None:
        workspace = make_temp_dir(self, prefix="cli-")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch(["animgen", "status", "--workspace", str(workspace)])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out.getvalue())["fix"], "waterfree animgen install")
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = dispatch(["animgen", "make", "a person walks", "--workspace", str(workspace)])
        self.assertEqual(code, 4)
        self.assertIn("waterfree animgen install", err.getvalue())

    def test_missing_build_tools_are_named_before_anything_is_built(self) -> None:
        with mock.patch("backend.engines.pyenv.find_uv", return_value=None), \
                mock.patch("backend.engines.pyenv.msvc_available", return_value=False), \
                self.assertRaises(InstallError) as caught:
            pyenv._check_prerequisites(kimodo.KIMODO)
        self.assertIn("uv", str(caught.exception))
        self.assertIn("C++", str(caught.exception))


class SupportTests(unittest.TestCase):
    def test_find_blender_prefers_config_then_the_newest_install(self) -> None:
        root = make_temp_dir(self, prefix="pf-")
        for version in ("Blender 4.2", "Blender 5.1", "Blender 5.0"):
            (root / "Blender Foundation" / version).mkdir(parents=True)
            (root / "Blender Foundation" / version / "blender.exe").write_bytes(b"")
        configured = root / "custom" / "blender.exe"
        configured.parent.mkdir()
        configured.write_bytes(b"")
        with mock.patch.dict(os.environ, {"ProgramFiles": str(root), "WATERFREE_BLENDER": ""}), \
                mock.patch("backend.animgen.fbx.shutil.which", return_value=None), \
                mock.patch("backend.animgen.fbx.sys.platform", "win32"):
            self.assertEqual(fbx_lib.find_blender(str(configured)), configured)
            self.assertEqual(fbx_lib.find_blender().parent.name, "Blender 5.1")

    def test_largest_programs_sums_processes_and_skips_system_ones(self) -> None:
        rows = [(1, "chrome.exe", 2 * 1024 ** 3), (2, "chrome.exe", 3 * 1024 ** 3), (3, "svchost.exe", 9 * 1024 ** 3),
                (4, "Unity.exe", 4 * 1024 ** 3), (os.getpid(), "python.exe", 9 * 1024 ** 3), (6, "tiny.exe", 10)]
        with mock.patch("backend.gpu.ram._windows_processes", return_value=rows), \
                mock.patch("backend.gpu.ram._proc_processes", return_value=rows):
            programs = ram.largest_programs()
        self.assertEqual([(p["name"], p["gb"], p["processes"]) for p in programs],
                         [("chrome.exe", 5.0, 2), ("Unity.exe", 4.0, 1)])

    def test_require_passes_with_enough_ram(self) -> None:
        with mock.patch("backend.gpu.ram.query", return_value=PLENTY):
            self.assertIs(ram.require(17.0, purpose="test"), PLENTY)


if __name__ == "__main__":
    unittest.main()
