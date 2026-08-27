import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.llm import ollama_client
from backend.vision import analyze as analyze_mod
from backend.vision import models as vision_models
from backend.vision.analyze import VisionError, analyze, prepare_image
from backend.vision.models import VisionModelMissing


def _write_png(path: Path, size: tuple[int, int]) -> Path:
    from PIL import Image

    Image.new("RGB", size, (30, 60, 120)).save(path)
    return path


class PrepareImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_oversized_image_is_downscaled_preserving_aspect(self) -> None:
        path = _write_png(self.dir / "big.png", (3840, 2160))

        prepared = prepare_image(str(path))

        self.assertTrue(prepared.was_downscaled)
        self.assertEqual(max(prepared.width, prepared.height), analyze_mod.MAX_EDGE_PX)
        self.assertAlmostEqual(prepared.width / prepared.height, 3840 / 2160, places=2)

    def test_small_image_is_left_alone(self) -> None:
        path = _write_png(self.dir / "small.png", (800, 600))

        prepared = prepare_image(str(path))

        self.assertFalse(prepared.was_downscaled)
        self.assertEqual((prepared.width, prepared.height), (800, 600))

    def test_missing_file_is_reported_clearly(self) -> None:
        with self.assertRaises(VisionError) as ctx:
            prepare_image(str(self.dir / "nope.png"))

        self.assertIn("not found", str(ctx.exception).lower())

    def test_unsupported_type_is_rejected_before_decoding(self) -> None:
        doc = self.dir / "report.pdf"
        doc.write_bytes(b"%PDF-1.4")

        with self.assertRaises(VisionError) as ctx:
            prepare_image(str(doc))

        self.assertIn("Unsupported", str(ctx.exception))


class PurposeRoutingTests(unittest.TestCase):
    def test_open_ended_description_uses_the_small_tier(self) -> None:
        self.assertEqual(vision_models.get_purpose("describe").tier, vision_models.SMALL)

    def test_triage_is_large_despite_being_a_cheap_job(self) -> None:
        """The small tier returns "" for verdict questions, so triage cannot use it."""
        self.assertEqual(vision_models.get_purpose("triage").tier, vision_models.LARGE)

    def test_analytical_purposes_use_the_large_tier(self) -> None:
        for key in ("ui", "function", "text", "compare"):
            purpose = vision_models.get_purpose(key)
            self.assertEqual(purpose.tier, vision_models.LARGE, key)

    def test_explicit_model_overrides_the_purpose(self) -> None:
        purpose = vision_models.get_purpose("describe")

        chosen = vision_models.resolve_model(purpose, override="llava:13b")

        self.assertEqual(chosen, "llava:13b")

    def test_unknown_purpose_lists_the_valid_ones(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            vision_models.get_purpose("vibes")

        self.assertIn("describe", str(ctx.exception))

    def test_missing_model_names_the_pull_command(self) -> None:
        """Nothing is ever downloaded implicitly, so the error must be actionable."""
        with mock.patch.object(ollama_client, "has_model", return_value=False):
            with self.assertRaises(VisionModelMissing) as ctx:
                vision_models.ensure_available("moondream")

        self.assertIn("waterfree vision pull --tier small", str(ctx.exception))


class AnalyzeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.image = _write_png(self.dir / "shot.png", (400, 300))

    def test_images_ride_on_the_last_user_message(self) -> None:
        with mock.patch.object(ollama_client, "has_model", return_value=True), \
                mock.patch.object(ollama_client, "chat", return_value="looks fine") as chat:
            result = analyze([str(self.image)], purpose="describe")

        self.assertEqual(result["answer"], "looks fine")
        self.assertEqual(len(chat.call_args.kwargs["images"]), 1)
        self.assertEqual(chat.call_args.kwargs["model"], "moondream")

    def test_multiple_images_are_numbered_in_the_prompt(self) -> None:
        second = _write_png(self.dir / "after.png", (400, 300))

        with mock.patch.object(ollama_client, "has_model", return_value=True), \
                mock.patch.object(ollama_client, "chat", return_value="diff") as chat:
            analyze([str(self.image), str(second)], purpose="compare")

        prompt = chat.call_args.kwargs["messages"][-1]["content"]
        self.assertIn("[image 1] shot.png", prompt)
        self.assertIn("[image 2] after.png", prompt)

    def test_custom_question_replaces_the_default(self) -> None:
        with mock.patch.object(ollama_client, "has_model", return_value=True), \
                mock.patch.object(ollama_client, "chat", return_value="blue") as chat:
            result = analyze([str(self.image)], purpose="describe",
                             question="What colour is the background?")

        self.assertIn("What colour", chat.call_args.kwargs["messages"][-1]["content"])
        self.assertEqual(result["question"], "What colour is the background?")

    def test_too_many_images_is_refused(self) -> None:
        many = [str(self.image)] * (analyze_mod.MAX_IMAGES + 1)

        with self.assertRaises(ValueError) as ctx:
            analyze(many, purpose="describe")

        self.assertIn("limit", str(ctx.exception))

    def test_no_images_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            analyze([], purpose="describe")


class AttachImagesTests(unittest.TestCase):
    def test_attaches_to_the_last_user_turn(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "second"},
        ]

        out = ollama_client._attach_images(messages, ["B64"])

        self.assertNotIn("images", out[1])
        self.assertEqual(out[3]["images"], ["B64"])

    def test_does_not_mutate_the_caller_list(self) -> None:
        messages = [{"role": "user", "content": "u"}]

        ollama_client._attach_images(messages, ["B64"])

        self.assertNotIn("images", messages[0])

    def test_synthesises_a_turn_rather_than_dropping_pixels(self) -> None:
        out = ollama_client._attach_images([{"role": "system", "content": "s"}], ["B64"])

        self.assertEqual(out[-1]["role"], "user")
        self.assertEqual(out[-1]["images"], ["B64"])

    def test_no_images_is_a_passthrough(self) -> None:
        messages = [{"role": "user", "content": "u"}]

        self.assertIs(ollama_client._attach_images(messages, None), messages)


if __name__ == "__main__":
    unittest.main()
