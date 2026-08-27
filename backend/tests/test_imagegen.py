import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.imagegen import backend_diffusers as backend
from backend.imagegen import generate as generate_mod
from backend.imagegen.config import (
    OFFLOAD_MODEL,
    OFFLOAD_SEQUENTIAL,
    PRESETS,
    ImageGenConfig,
    apply_overrides,
    fits_vram,
    load_config,
    write_default_config,
)


class PresetTests(unittest.TestCase):
    def test_the_default_preset_fits_a_12gb_card(self) -> None:
        self.assertTrue(fits_vram(PRESETS["sd35-medium"], 12.0))

    def test_flux_does_not_claim_to_fit_12gb(self) -> None:
        """It only runs here with sequential offload, so `fitsGpu` must say no."""
        self.assertFalse(fits_vram(PRESETS["flux-schnell"], 12.0))

    def test_distilled_presets_carry_zero_guidance(self) -> None:
        """Turbo/schnell burn the image if given a normal guidance scale."""
        for key in ("sdxl-turbo", "flux-schnell"):
            self.assertEqual(PRESETS[key].guidance, 0.0, key)

    def test_sd35_uses_bfloat16(self) -> None:
        """float16 is a known source of black output for SD3."""
        self.assertEqual(PRESETS["sd35-medium"].dtype, "bfloat16")

    def test_no_gpu_means_nothing_fits(self) -> None:
        self.assertFalse(fits_vram(PRESETS["sdxl-turbo"], 0.0))


class ConfigLayeringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_preset_supplies_the_values_no_one_overrode(self) -> None:
        spec = ImageGenConfig(preset="sdxl").merged()

        self.assertEqual(spec.steps, PRESETS["sdxl"].steps)
        self.assertEqual(spec.guidance, PRESETS["sdxl"].guidance)
        self.assertEqual(spec.width, PRESETS["sdxl"].width)

    def test_overrides_beat_the_preset(self) -> None:
        spec = ImageGenConfig(preset="sdxl", steps=12, width=768).merged()

        self.assertEqual(spec.steps, 12)
        self.assertEqual(spec.width, 768)
        self.assertEqual(spec.height, PRESETS["sdxl"].height)

    def test_zero_guidance_is_an_override_not_an_absence(self) -> None:
        """0.0 is meaningful (distilled models), so the sentinel must be negative."""
        spec = ImageGenConfig(preset="sdxl", guidance=0.0).merged()

        self.assertEqual(spec.guidance, 0.0)

    def test_unsupplied_cli_flags_do_not_clear_config_values(self) -> None:
        config = ImageGenConfig(preset="sdxl", steps=15)

        merged = apply_overrides(config, steps=None, width=512)

        self.assertEqual(merged.steps, 15)
        self.assertEqual(merged.width, 512)

    def test_workspace_config_is_read_and_camel_cased(self) -> None:
        path = Path(self.workspace) / ".waterfree" / "imagegen.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"preset": "sdxl-turbo", "negativePrompt": "blurry"}),
                        encoding="utf-8")

        config = load_config(self.workspace)

        self.assertEqual(config.preset, "sdxl-turbo")
        self.assertEqual(config.negative_prompt, "blurry")

    def test_a_broken_config_falls_back_to_defaults(self) -> None:
        path = Path(self.workspace) / ".waterfree" / "imagegen.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        self.assertEqual(load_config(self.workspace), ImageGenConfig())

    def test_unknown_keys_are_ignored_rather_than_fatal(self) -> None:
        path = Path(self.workspace) / ".waterfree" / "imagegen.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"preset": "sdxl", "lora": "nope"}), encoding="utf-8")

        self.assertEqual(load_config(self.workspace).preset, "sdxl")

    def test_init_round_trips_through_load(self) -> None:
        write_default_config(self.workspace)

        self.assertEqual(load_config(self.workspace), ImageGenConfig())

    def test_unknown_preset_lists_the_valid_ones(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ImageGenConfig(preset="midjourney").merged()

        self.assertIn("sd35-medium", str(ctx.exception))


class MemoryStrategyTests(unittest.TestCase):
    def test_model_offload_never_also_moves_the_pipeline_to_cuda(self) -> None:
        """`.to("cuda")` would defeat the offload hooks and reintroduce the OOM."""
        spec = ImageGenConfig(preset="sdxl", offload=OFFLOAD_MODEL).merged()
        pipeline = mock.MagicMock()

        backend._apply_memory_strategy(pipeline, spec)

        pipeline.enable_model_cpu_offload.assert_called_once()
        pipeline.to.assert_not_called()

    def test_sequential_offload_is_selected_verbatim(self) -> None:
        spec = ImageGenConfig(preset="sdxl", offload=OFFLOAD_SEQUENTIAL).merged()
        pipeline = mock.MagicMock()

        backend._apply_memory_strategy(pipeline, spec)

        pipeline.enable_sequential_cpu_offload.assert_called_once()
        pipeline.enable_model_cpu_offload.assert_not_called()

    def test_no_offload_moves_the_pipeline_to_the_gpu(self) -> None:
        spec = ImageGenConfig(preset="sdxl", offload="none").merged()
        pipeline = mock.MagicMock()

        backend._apply_memory_strategy(pipeline, spec)

        pipeline.to.assert_called_once_with("cuda")

    def test_a_pipeline_missing_an_optimisation_is_not_fatal(self) -> None:
        spec = ImageGenConfig(preset="sdxl", offload="none").merged()
        pipeline = mock.MagicMock(spec=["to"])  # no enable_* methods at all

        backend._apply_memory_strategy(pipeline, spec)  # must not raise

    def test_negative_prompt_is_dropped_for_distilled_models(self) -> None:
        distilled = ImageGenConfig(preset="sdxl-turbo", negative_prompt="blurry").merged()
        guided = ImageGenConfig(preset="sdxl", negative_prompt="blurry").merged()

        self.assertFalse(backend._supports_negative_prompt(distilled))
        self.assertTrue(backend._supports_negative_prompt(guided))


class HintTests(unittest.TestCase):
    def test_gated_repo_error_explains_the_licence_and_token(self) -> None:
        spec = ImageGenConfig(preset="sd35-medium").merged()

        hint = backend._download_hint(spec, Exception("401 Client Error: gated repo"))

        self.assertIn("HF_TOKEN", hint)
        self.assertIn("licence", hint)

    def test_oom_hint_suggests_cheaper_settings_and_freeing_ollama(self) -> None:
        spec = ImageGenConfig(preset="sd35-medium").merged()

        hint = backend._oom_hint(spec)

        self.assertIn("sequential", hint)
        self.assertIn("ollama stop", hint)


class GenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _fake_images(self, count: int) -> list:
        from PIL import Image

        return [Image.new("RGB", (8, 8)) for _ in range(count)]

    def test_each_image_gets_a_reproducibility_sidecar(self) -> None:
        with mock.patch.object(generate_mod.backend, "require_available"), \
                mock.patch.object(generate_mod.backend, "load_pipeline"), \
                mock.patch.object(generate_mod.backend, "run_pipeline",
                                  return_value=self._fake_images(2)):
            result = generate_mod.generate(
                "a red cube", config=ImageGenConfig(preset="sdxl", seed=7),
                workspace_path=self.workspace, count=2,
            )

        self.assertEqual(result["count"], 2)
        for entry in result["files"]:
            self.assertTrue(Path(entry["image"]).exists())
            meta = json.loads(Path(entry["metadata"]).read_text(encoding="utf-8"))
            self.assertEqual(meta["prompt"], "a red cube")

    def test_only_the_first_image_of_a_batch_records_the_seed(self) -> None:
        """A batch shares one generator, so a recorded seed reproduces image 1 only."""
        with mock.patch.object(generate_mod.backend, "require_available"), \
                mock.patch.object(generate_mod.backend, "load_pipeline"), \
                mock.patch.object(generate_mod.backend, "run_pipeline",
                                  return_value=self._fake_images(2)):
            result = generate_mod.generate(
                "x", config=ImageGenConfig(preset="sdxl", seed=7),
                workspace_path=self.workspace, count=2,
            )

        seeds = [
            json.loads(Path(f["metadata"]).read_text(encoding="utf-8"))["seed"]
            for f in result["files"]
        ]
        self.assertEqual(seeds, [7, None])

    def test_filenames_are_slugged_from_the_prompt(self) -> None:
        with mock.patch.object(generate_mod.backend, "require_available"), \
                mock.patch.object(generate_mod.backend, "load_pipeline"), \
                mock.patch.object(generate_mod.backend, "run_pipeline",
                                  return_value=self._fake_images(1)):
            result = generate_mod.generate(
                "A Red Cube, on grass!", config=ImageGenConfig(preset="sdxl"),
                workspace_path=self.workspace,
            )

        self.assertIn("a-red-cube-on-grass", Path(result["files"][0]["image"]).name)

    def test_an_empty_prompt_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            generate_mod.generate("   ", config=ImageGenConfig(), workspace_path=self.workspace)

    def test_batch_size_is_capped(self) -> None:
        with self.assertRaises(ValueError):
            generate_mod.generate("x", config=ImageGenConfig(),
                                  workspace_path=self.workspace,
                                  count=generate_mod.MAX_BATCH + 1)


if __name__ == "__main__":
    unittest.main()
