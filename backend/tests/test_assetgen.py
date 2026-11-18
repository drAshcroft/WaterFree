"""meshgen and audiogen pipelines against a stub engine (no GPU, no weights)."""

import io
import json
import struct
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from backend.audiogen import generate as audio
from backend.audiogen import voices as voice_lib
from backend.audiogen import wav
from backend.audiogen.config import AudiogenConfig
from backend.cli.dispatcher import dispatch
from backend.engines.catalog import ENGINES, MODEL_SETS
from backend.meshgen import generate as mesh
from backend.meshgen.config import MeshgenConfig
from backend.test_support import make_temp_dir


def make_glb(triangles: int = 12, vertices: int = 8) -> bytes:
    gltf = {
        "asset": {"version": "2.0"},
        "accessors": [{"count": vertices}, {"count": triangles * 3}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "materials": [{}], "textures": [{}, {}], "extensionsUsed": ["EXT_texture_webp"],
    }
    chunk = json.dumps(gltf).encode()
    chunk += b" " * (-len(chunk) % 4)
    body = struct.pack("<II", len(chunk), 0x4E4F534A) + chunk
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def make_wav(seconds: float = 3.0, rate: int = 24000, channels: int = 1) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        frames = int(seconds * rate)
        writer.writeframes(b"".join(struct.pack("<h", (i % 200) - 100) * channels for i in range(frames)))
    return out.getvalue()


class StubServer:
    """Stands in for EngineServer: records requests, answers with canned bytes."""

    instances: list["StubServer"] = []

    def __init__(self, *, name, argv, cwd=None, report=None, echo_log=False, env=None, **_):
        self.name, self.argv, self.env = name, argv, env or {}
        self.requests: list[tuple[str, dict]] = []
        self.load_seconds = 1.0
        self.log_path = None
        StubServer.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def running(self):
        return True

    def log_tail(self):
        return ""

    def spill_report(self):
        return {"vramSpillMb": 410, "sharedPeakMb": 480, "dedicatedPeakMb": 11000, "spilled": True}

    def post_multipart(self, path, *, fields, files, timeout):
        self.requests.append((path, dict(fields)))
        return make_glb(), {}

    def post_json(self, path, payload, *, timeout):
        self.requests.append((path, payload))
        rate, channels = (48000, 2) if self.name.endswith("speech") else (24000 if self.name.endswith("voice") else 48000, 1)
        return make_wav(rate=rate, channels=channels), {"x-moss-generate-seconds": "0.5", "x-moss-prompt": "p"}


class AssetgenTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = make_temp_dir(self, prefix="assetgen-")
        StubServer.instances = []
        self.messages: list[str] = []
        for target in ("backend.meshgen.generate.EngineServer", "backend.audiogen.generate.EngineServer"):
            patcher = mock.patch(target, StubServer)
            patcher.start()
            self.addCleanup(patcher.stop)

        def fake_require(engine_key, model_key, *, install_hint):
            return ENGINES[engine_key], MODEL_SETS[model_key]

        for target in ("backend.meshgen.generate.install.require", "backend.audiogen.generate.install.require"):
            patcher = mock.patch(target, side_effect=fake_require)
            patcher.start()
            self.addCleanup(patcher.stop)


class MeshgenTests(AssetgenTestCase):
    def image(self, name="chest.png") -> Path:
        from PIL import Image
        path = self.workspace / name
        Image.new("RGB", (64, 64), "grey").save(path)
        return path

    def test_make_writes_glb_and_sidecar_with_provenance(self) -> None:
        result = mesh.make([str(self.image())], config=MeshgenConfig(seed=5), workspace=str(self.workspace),
                           report=self.messages.append)
        self.assertEqual(result["count"], 1)
        entry = result["files"][0]
        self.assertTrue(Path(entry["mesh"]).read_bytes().startswith(b"glTF"))
        self.assertEqual(entry["triangles"], 12)
        sidecar = json.loads(Path(entry["metadata"]).read_text())
        self.assertEqual(sidecar["seed"], 5)
        self.assertEqual(sidecar["engine"]["version"], ENGINES["trellis"].version)
        self.assertIn("DINOv3", sidecar["licence"])
        self.assertEqual(len(sidecar["input"]["sha256"]), 64)
        self.assertIn(str(self.workspace / ".waterfree" / "generated" / "meshes"), entry["mesh"])
        self.assertEqual(result["vramSpill"]["vramSpillMb"], 410, "GPU-005: a spill shows in the result")

    def test_server_flags_and_request_fields_follow_the_config(self) -> None:
        config = MeshgenConfig(resolution=512, texture=False, uv="box", bg_removal="birefnet", seed=1)
        mesh.make([str(self.image())], config=config, workspace=str(self.workspace), report=self.messages.append)
        server = StubServer.instances[0]
        argv = server.argv
        self.assertEqual(argv[argv.index("--res") + 1], "512")
        self.assertIn("--no-texture", argv)
        self.assertIn("--box-uv", argv)
        self.assertNotIn("--faces", argv, "trellis.cpp v0.8.1 rejects --faces at launch")
        path, fields = server.requests[0]
        self.assertEqual(path, "/generate")
        self.assertEqual(fields, {"seed": "1", "resolution": "512", "bg_removal": "birefnet"})

    def test_a_triangle_budget_is_refused_rather_than_silently_ignored(self) -> None:
        """v0.8.1 has a fixed QEM target; promising --faces and ignoring it shipped 7x the triangles."""
        with self.assertRaisesRegex(ValueError, "GPU-009"):
            mesh.make([str(self.image())], config=MeshgenConfig(faces=20000), workspace=str(self.workspace))
        self.assertEqual(StubServer.instances, [])

    def test_one_server_serves_the_whole_batch(self) -> None:
        images = [str(self.image(f"{i}.png")) for i in range(3)]
        result = mesh.make(images, config=MeshgenConfig(), workspace=str(self.workspace), report=self.messages.append)
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(StubServer.instances), 1)
        seeds = {f["seed"] for f in result["files"]}
        self.assertEqual(len(seeds), 3, "random seeds are drawn per image and recorded")

    def test_bad_input_fails_before_the_gpu_is_touched(self) -> None:
        bogus = self.workspace / "not-an-image.png"
        bogus.write_text("hello")
        with self.assertRaises(ValueError):
            mesh.make([str(bogus)], config=MeshgenConfig(), workspace=str(self.workspace))
        self.assertEqual(StubServer.instances, [])

    def test_config_validation(self) -> None:
        for bad in (MeshgenConfig(resolution=768), MeshgenConfig(faces=10), MeshgenConfig(uv="planar"),
                    MeshgenConfig(steps=0)):
            with self.assertRaises(ValueError):
                bad.validate()

    def test_glb_stats_reads_the_json_chunk(self) -> None:
        stats = mesh.glb_stats(make_glb(triangles=20000, vertices=10500))
        self.assertEqual((stats["triangles"], stats["vertices"], stats["textures"]), (20000, 10500, 2))
        self.assertEqual(mesh.glb_stats(b"not a glb"), {})


class MeshPreviewTests(unittest.TestCase):
    """GPU-006: GLB -> PNG views. The browser itself is exercised live, not here."""

    def test_the_vendored_viewer_matches_its_pin(self) -> None:
        from backend.meshgen import preview
        preview.check_viewer()   # raises if the file is missing or altered

    def test_inputs_are_checked_before_a_browser_starts(self) -> None:
        from backend.meshgen import preview
        folder = make_temp_dir(self, prefix="preview-")
        not_glb = folder / "x.glb"
        not_glb.write_bytes(b"not a glb")
        with mock.patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            for bad in (str(folder / "missing.glb"), str(not_glb)):
                with self.assertRaises(ValueError):
                    preview.render([bad])
            with self.assertRaises(ValueError):
                preview.render([str(not_glb)], size=64)

    def test_grid_is_a_labelled_two_by_two(self) -> None:
        from PIL import Image

        from backend.meshgen import preview
        folder = make_temp_dir(self, prefix="preview-")
        views = {}
        for name, colour in zip(preview.VIEWS, ("red", "green", "blue", "white"), strict=True):
            path = folder / f"{name}.png"
            Image.new("RGB", (64, 64), colour).save(path)
            views[name] = str(path)
        grid = preview.make_grid(views, folder / "grid.png")
        with Image.open(grid) as image:
            self.assertEqual(image.size, (128, 128))
            self.assertEqual(image.getpixel((40, 40))[:3], (255, 0, 0))
            self.assertEqual(image.getpixel((100, 100))[:3], (255, 255, 255))

    def test_cli_preview_reports_grids_and_failures(self) -> None:
        rendered = [{"glb": "a.glb", "grid": "a.preview.png", "views": {}}, {"glb": "b.glb", "error": "boom"}]
        out = io.StringIO()
        with mock.patch("backend.meshgen.preview.render", return_value=rendered), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = dispatch(["meshgen", "preview", "a.glb", "b.glb"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual((payload["count"], len(payload["failed"])), (1, 1))


class AudiogenTests(AssetgenTestCase):
    def config(self, **kw) -> AudiogenConfig:
        return AudiogenConfig(**kw)

    def test_design_writes_distinct_seeded_candidates(self) -> None:
        result = audio.design("gruff tavern keeper", "Welcome, traveller.", count=3, seed=100,
                              config=self.config(), workspace=str(self.workspace), report=self.messages.append)
        self.assertEqual([f["seed"] for f in result["files"]], [100, 101, 102])
        server = StubServer.instances[0]
        self.assertIn("--template", server.argv)
        self.assertEqual(server.argv[server.argv.index("--template") + 1], "voicegen")
        self.assertTrue(server.argv[server.argv.index("--model") + 1].endswith("moss-voicegen.gguf"))
        payload = server.requests[0][1]
        self.assertEqual(payload["instruction"], "gruff tavern keeper")
        sidecar = json.loads(Path(result["files"][0]["metadata"]).read_text())
        self.assertEqual(sidecar["transcript"], "Welcome, traveller.")

    def test_saved_voice_is_cloned_for_every_line(self) -> None:
        designed = audio.design("old sailor", "Ahoy there, landlubber, mind the rigging.", count=1, seed=1,
                                config=self.config(), workspace=str(self.workspace), report=self.messages.append)
        candidate = Path(designed["files"][0]["audio"])
        library = self.workspace / ".waterfree" / "voices"
        voice = voice_lib.save(library, "sailor", candidate, description="old sailor",
                               transcript="Ahoy there, landlubber, mind the rigging.")
        lines = [{"text": "Fair winds."}, {"text": "Batten down!", "name": "sailor_storm"}]
        result = audio.speak(lines, voice="sailor", seed=7, config=self.config(), workspace=str(self.workspace),
                             report=self.messages.append)
        self.assertEqual(result["count"], 2)
        speech = StubServer.instances[-1]
        # The server's registry reads the workspace library directly and caches
        # the encoded reference, instead of re-encoding it for every line.
        self.assertEqual(speech.argv[speech.argv.index("--voice-dir") + 1], str(library))
        self.assertTrue(all(p["voice"] == "sailor" and "reference_wav_b64" not in p for _, p in speech.requests))
        self.assertEqual([p["sampling"]["seed"] for _, p in speech.requests], [7, 8])
        self.assertTrue(all(p["max_new_tokens"] == 2048 for _, p in speech.requests))
        self.assertEqual(speech.argv[speech.argv.index("--n-ctx") + 1], "4096")
        self.assertTrue(result["files"][1]["audio"].endswith("sailor_storm.wav"))
        sidecar = json.loads(Path(result["files"][0]["metadata"]).read_text())
        self.assertEqual(sidecar["voice"]["sha256"], voice.record["sha256"])

    def test_voice_library_records_match_the_engine_registry_format(self) -> None:
        """openmoss --voice-dir reads <id>.json's transcript/description; keep those keys."""
        wav_path = self.workspace / "ref.wav"
        wav_path.write_bytes(make_wav(seconds=3.0))
        library = self.workspace / "voices"
        voice_lib.save(library, "smith", wav_path, description="burly smith", transcript="Hot iron!")
        record = json.loads((library / "smith.json").read_text())
        self.assertEqual((record["transcript"], record["description"]), ("Hot iron!", "burly smith"))
        self.assertTrue((library / "smith.wav").is_file())

    def test_unknown_voice_fails_before_the_gpu_is_touched(self) -> None:
        with self.assertRaises(voice_lib.VoiceError):
            audio.speak([{"text": "hi"}], voice="nobody", seed=0, config=self.config(),
                        workspace=str(self.workspace))
        self.assertEqual(StubServer.instances, [])

    def test_speech_can_be_downmixed_for_positional_audio(self) -> None:
        result = audio.speak([{"text": "hi"}], voice=None, seed=0, config=self.config(channels="mono"),
                             workspace=str(self.workspace), report=self.messages.append)
        info = wav.info(Path(result["files"][0]["audio"]))
        self.assertEqual(info["channels"], 1)
        sidecar = json.loads(Path(result["files"][0]["metadata"]).read_text())
        self.assertEqual(sidecar["audio"]["nativeChannels"], 2)

    def test_sfx_variants_get_consecutive_seeds(self) -> None:
        result = audio.sfx(["door creak", "sword clash"], count=2, seed=10, config=self.config(sfx_seconds=2.5),
                           workspace=str(self.workspace), report=self.messages.append)
        self.assertEqual(result["count"], 4)
        server = StubServer.instances[0]
        self.assertEqual([p["seed"] for _, p in server.requests], [10, 11, 12, 13])
        self.assertTrue(all(p["seconds"] == 2.5 and p["steps"] == 100 for _, p in server.requests))

    def test_each_role_runs_on_its_model_sets_engine_build(self) -> None:
        """SFX on openmoss's Vulkan build (its CUDA VAE decode is ~66x slower), speech on CUDA."""
        audio.sfx(["thud"], count=1, seed=1, config=self.config(), workspace=str(self.workspace),
                  report=self.messages.append)
        audio.speak([{"text": "hi"}], voice=None, seed=0, config=self.config(), workspace=str(self.workspace),
                    report=self.messages.append)
        sfx_server, speech_server = StubServer.instances
        self.assertIn(str(ENGINES["openmoss-vulkan"].install_dir()), sfx_server.argv[0])
        self.assertEqual(sfx_server.env.get("GGML_VK_DISABLE_F16"), "1")
        self.assertIn(str(ENGINES["openmoss"].install_dir()), speech_server.argv[0])
        self.assertNotIn("GGML_VK_DISABLE_F16", speech_server.env)

    def test_lines_file_formats(self) -> None:
        txt = self.workspace / "lines.txt"
        txt.write_text("First line.\n\nSecond line.\n", encoding="utf-8")
        self.assertEqual(audio.parse_lines_file(str(txt)), [{"text": "First line."}, {"text": "Second line."}])
        js = self.workspace / "lines.json"
        js.write_text(json.dumps(["Plain.", {"text": "Named.", "voice": "smith", "name": "smith_greeting",
                                             "seed": 3}, {"text": "Odd.", "name": "Act 2: ambush!"}]),
                      encoding="utf-8")
        self.assertEqual(audio.parse_lines_file(str(js)), [
            {"text": "Plain."},
            {"text": "Named.", "voice": "smith", "name": "smith_greeting", "seed": 3},
            {"text": "Odd.", "name": "Act-2-ambush"},
        ])

    def test_duplicate_line_names_are_refused(self) -> None:
        js = self.workspace / "dupes.json"
        js.write_text(json.dumps([{"text": "a", "name": "bark"}, {"text": "b", "name": "bark"}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            audio.parse_lines_file(str(js))

    def test_voice_names_are_safe_file_names(self) -> None:
        for bad in ("", "../etc", "Two Words", "x" * 65):
            with self.assertRaises(voice_lib.VoiceError):
                voice_lib.check_name(bad)
        self.assertEqual(voice_lib.check_name("Old-Sailor"), "old-sailor")

    def test_voice_clip_length_is_checked(self) -> None:
        short = self.workspace / "short.wav"
        short.write_bytes(make_wav(seconds=0.5))
        with self.assertRaises(voice_lib.VoiceError):
            voice_lib.save(self.workspace / "voices", "tiny", short)

    def test_wav_channel_round_trip(self) -> None:
        mono = make_wav(seconds=0.1, channels=1)
        stereo = wav.convert_channels(mono, "stereo")
        self.assertEqual(wav.info(stereo)["channels"], 2)
        self.assertEqual(wav.convert_channels(stereo, "mono"), mono)
        self.assertIs(wav.convert_channels(mono, "native"), mono)


class AudioQaTests(AssetgenTestCase):
    """GPU-007: opt-in Whisper/CLAP checks after generation, inside the same lease."""

    def setUp(self) -> None:
        super().setUp()
        self.qa_requests: list[list[dict]] = []
        self.answers: dict[str, dict] = {}
        test = self

        class QaServer(StubServer):
            def post_json(self, path, payload, *, timeout):
                if path != "/qa":
                    return super().post_json(path, payload, timeout=timeout)
                test.qa_requests.append(payload["items"])
                results = [{"id": item["id"], **test.answers.get(item["wav"], {"transcript": item.get("text") or "",
                                                                             "clapScore": 0.3})}
                           for item in payload["items"]]
                return json.dumps({"results": results}).encode(), {}

        for target in ("backend.audiogen.generate.EngineServer", "backend.audiogen.qa.EngineServer"):
            patcher = mock.patch(target, QaServer)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("backend.audiogen.qa.require", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, **kw) -> AudiogenConfig:
        return AudiogenConfig(**kw)

    def test_speech_scoring(self) -> None:
        from backend.audiogen.qa import score_speech
        perfect = score_speech("Fair winds, sailor!", "fair winds sailor")
        self.assertEqual((perfect["wer"], perfect["review"]), (0.0, False))
        dropped = score_speech("Hand me the hammer and the tongs", "hand me the hammer and tongs")
        self.assertEqual(dropped["dropped"], ["the"])
        self.assertFalse(dropped["review"], "one dropped article in seven words is not worth a listen")
        stutter = score_speech("go now", "go go now")
        self.assertEqual((stutter["repeated"], stutter["added"]), (["go"], []))
        self.assertTrue(stutter["review"])
        self.assertEqual(score_speech("I'll come", "I’ll come")["wer"], 0.0)

    def test_a_misread_word_is_flagged_but_a_spelling_variant_is_not(self) -> None:
        """Real takes, 2026-11-18: the misread candidate's WER (0.105) was under any overall threshold."""
        from backend.audiogen.qa import score_speech
        line = "Welcome to the Rusty Anchor, traveller! Pull up a stool, the stew is hot and the ale is cold."
        misread = score_speech(line, "Welcome to the Rusty Anchor. Trudler, pull up a stool. "
                                     "The stool is hot and the ale is cold.")
        self.assertEqual(misread["misread"], [["traveller", "trudler"], ["stew", "stool"]])
        self.assertTrue(misread["review"])
        variant = score_speech(line, "Welcome to the Rusty Anchor. Traveler, pull up a stool. "
                                     "The stew is hot and the ale is cold.")
        self.assertEqual((variant["spellingVariants"], variant["errors"]), ([["traveller", "traveler"]], 0))
        self.assertFalse(variant["review"])

    def fake_transcripts(self, transcripts):
        """Give the k-th checked take the k-th fake Whisper answer (the files only exist once generated)."""
        real_run = audio.audio_qa.run

        def capture(items, *, report):
            for item, answer in zip(items, transcripts, strict=True):
                self.answers[item["entry"]["audio"]] = answer
            return real_run(items, report=report)
        return mock.patch.object(audio.audio_qa, "run", side_effect=capture)

    def test_design_candidates_are_ranked_by_how_well_they_read_the_line(self) -> None:
        text = "Welcome, traveller, the stew is hot."
        heard = [{"transcript": "welcome traveller the stool is hot"}, {"transcript": text},
                 {"transcript": "welcome welcome traveller the stew"}]
        with self.fake_transcripts(heard):
            result = audio.design("innkeeper", text, count=3, seed=0, config=self.config(),
                                  workspace=str(self.workspace), qa=True, report=self.messages.append)
        ranks = [f["qa"]["rank"] for f in result["files"]]
        self.assertEqual(ranks, [2, 1, 3])
        self.assertEqual(result["qa"]["best"], [result["files"][1]["audio"]])
        self.assertEqual(set(result["qa"]["review"]), {result["files"][0]["audio"], result["files"][2]["audio"]})
        sidecar = json.loads(Path(result["files"][1]["metadata"]).read_text())
        self.assertEqual(sidecar["qa"]["wer"], 0.0)

    def test_qa_runs_after_openmoss_stops_inside_the_lease(self) -> None:
        events: list[str] = []
        real_exit = StubServer.__exit__

        def tracking_exit(server, *exc):
            events.append(f"stop {server.name}")
            return real_exit(server, *exc)

        with mock.patch.object(StubServer, "__exit__", tracking_exit), \
                mock.patch("backend.audiogen.generate.exclusive_gpu", wraps=audio.exclusive_gpu) as lease:
            audio.speak([{"text": "hi there"}], voice=None, seed=0, config=self.config(), workspace=str(self.workspace),
                        qa=True, report=self.messages.append)
        self.assertEqual(events, ["stop openmoss-speech", "stop audioqa"])
        self.assertEqual(lease.call_count, 1, "one lease covers generation and QA")

    def test_sfx_variants_are_ranked_per_prompt(self) -> None:
        with self.fake_transcripts([{"clapScore": 0.21}, {"clapScore": 0.35}, {"clapScore": 0.10}]):
            result = audio.sfx(["door creak"], count=3, seed=1, config=self.config(), workspace=str(self.workspace),
                               qa=True, report=self.messages.append)
        self.assertEqual([f["qa"]["rank"] for f in result["files"]], [2, 1, 3])
        self.assertEqual(self.qa_requests[0][0]["kind"], "sfx")

    def test_without_qa_nothing_extra_runs(self) -> None:
        result = audio.sfx(["thud"], count=1, seed=1, config=self.config(), workspace=str(self.workspace),
                           report=self.messages.append)
        self.assertNotIn("qa", result)
        self.assertEqual(self.qa_requests, [])

    def test_qa_not_installed_fails_before_the_gpu(self) -> None:
        from backend.engines.install import InstallError
        with mock.patch("backend.audiogen.qa.require", side_effect=InstallError("not installed")), \
                self.assertRaises(InstallError):
            audio.sfx(["thud"], count=1, seed=1, config=self.config(), workspace=str(self.workspace), qa=True)
        self.assertEqual(StubServer.instances, [])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch(list(argv))
        return code, out.getvalue()

    def test_status_says_how_to_install(self) -> None:
        workspace = make_temp_dir(self, prefix="cli-")
        code, out = self.run_cli("meshgen", "status", "--workspace", str(workspace))
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["fix"], "waterfree meshgen install")
        code, out = self.run_cli("audiogen", "status", "--workspace", str(workspace))
        self.assertEqual(code, 4)
        self.assertIn("waterfree audiogen install", json.loads(out)["fix"])

    def test_make_without_install_exits_4(self) -> None:
        workspace = make_temp_dir(self, prefix="cli-")
        from PIL import Image
        image = workspace / "a.png"
        Image.new("RGB", (8, 8)).save(image)
        code, _ = self.run_cli("meshgen", "make", str(image), "--workspace", str(workspace))
        self.assertEqual(code, 4)

    def test_gpu_status_reports_lease_and_installs(self) -> None:
        code, out = self.run_cli("gpu", "status")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("lease", payload)
        self.assertEqual(set(payload["engines"]), set(ENGINES) | {"kimodo", "audioqa"})

    def test_voices_lists_an_empty_library(self) -> None:
        workspace = make_temp_dir(self, prefix="cli-")
        code, out = self.run_cli("audiogen", "voices", "--workspace", str(workspace))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["voices"], [])


if __name__ == "__main__":
    unittest.main()
