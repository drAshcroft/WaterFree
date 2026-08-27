import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.llm import openrouter_catalog as catalog


def _entry(model_id: str, *, prompt: str, completion: str, context: int = 128_000,
           tools: bool = True, outputs: list[str] | None = None) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "context_length": context,
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["tools"] if tools else [],
        "architecture": {
            "input_modalities": ["text"],
            "output_modalities": outputs or ["text"],
        },
    }


_PAYLOAD = {"data": [
    _entry("vendor/free-wide:free", prompt="0", completion="0", context=128_000),
    _entry("vendor/free-narrow:free", prompt="0", completion="0", context=32_000),
    _entry("vendor/free-tiny:free", prompt="0", completion="0", context=4_000),
    _entry("vendor/cheap", prompt="0.00000005", completion="0.0000002"),
    _entry("vendor/dear", prompt="0.00001", completion="0.00003"),
    _entry("vendor/variable", prompt="-1", completion="-1"),
    # Per-song billing, reported as per-token "0" with a huge context window.
    _entry("vendor/music-preview", prompt="0", completion="0",
           context=1_048_576, outputs=["text", "audio"]),
]}


class SelectCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.models = catalog.parse_models(_PAYLOAD)

    def test_free_prefers_widest_context_and_keeps_a_paid_tail(self) -> None:
        picks = catalog.select_candidates(self.models, sentinel=catalog.AUTO_FREE)

        self.assertEqual(picks[0], "vendor/free-wide:free")
        self.assertEqual(picks[1], "vendor/free-narrow:free")
        # The paid tail exists so a rate-limited free tier degrades rather than fails.
        self.assertIn("vendor/cheap", picks)
        self.assertLess(picks.index("vendor/cheap"), picks.index("vendor/dear"))

    def test_floor_is_cheapest_first_and_excludes_free(self) -> None:
        picks = catalog.select_candidates(self.models, sentinel=catalog.AUTO_FLOOR)

        self.assertEqual(picks[0], "vendor/cheap")
        self.assertNotIn("vendor/free-wide:free", picks)

    def test_variable_pricing_is_never_treated_as_free(self) -> None:
        """A "-1" price means "unknown", which must not read as zero."""
        picks = catalog.select_candidates(self.models, sentinel=catalog.AUTO_FREE)

        self.assertNotIn("vendor/variable", picks)

    def test_context_window_floor_drops_unusable_models(self) -> None:
        picks = catalog.select_candidates(self.models, sentinel=catalog.AUTO_FREE)

        self.assertNotIn("vendor/free-tiny:free", picks)

    def test_media_models_are_never_picked(self) -> None:
        """A song generator reports free per-token pricing and a 1M context."""
        picks = catalog.select_candidates(self.models, sentinel=catalog.AUTO_FREE)

        self.assertNotIn("vendor/music-preview", picks)
        self.assertEqual(picks[0], "vendor/free-wide:free")

    def test_rejects_a_non_sentinel(self) -> None:
        with self.assertRaises(ValueError):
            catalog.select_candidates(self.models, sentinel="anthropic/claude-sonnet-4.5")


class IsAutoModelTests(unittest.TestCase):
    def test_recognises_both_sentinels_case_insensitively(self) -> None:
        self.assertTrue(catalog.is_auto_model("auto:free"))
        self.assertTrue(catalog.is_auto_model("AUTO:FLOOR"))
        self.assertFalse(catalog.is_auto_model("qwen/qwen3-coder"))


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _cache_file(self) -> Path:
        return Path(self.workspace) / ".waterfree" / catalog.CACHE_FILENAME

    def test_fetch_is_written_to_cache_and_reused(self) -> None:
        with mock.patch.object(catalog, "_fetch_models",
                               return_value=catalog.parse_models(_PAYLOAD)) as fetch:
            catalog.load_models(workspace_path=self.workspace)
            self.assertTrue(self._cache_file().exists())
            catalog.load_models(workspace_path=self.workspace)

        self.assertEqual(fetch.call_count, 1)

    def test_stale_cache_is_served_when_the_fetch_fails(self) -> None:
        """Offline must not break a run that worked yesterday."""
        self._cache_file().parent.mkdir(parents=True, exist_ok=True)
        self._cache_file().write_text(json.dumps({
            "fetchedAt": time.time() - 10 * 24 * 3600,
            "data": _PAYLOAD["data"],
        }), encoding="utf-8")

        with mock.patch.object(catalog, "_fetch_models",
                               side_effect=catalog.CatalogUnavailable("offline")):
            models = catalog.load_models(workspace_path=self.workspace)

        self.assertIn("vendor/free-wide:free", [m.id for m in models])

    def test_failure_with_no_cache_raises(self) -> None:
        with mock.patch.object(catalog, "_fetch_models",
                               side_effect=catalog.CatalogUnavailable("offline")):
            with self.assertRaises(catalog.CatalogUnavailable):
                catalog.load_models(workspace_path=self.workspace)


class ParseModelsTests(unittest.TestCase):
    def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        models = catalog.parse_models({"data": [
            "not-a-dict",
            {"id": ""},
            {"id": "vendor/ok", "context_length": "65536", "pricing": {}},
        ]})

        self.assertEqual([m.id for m in models], ["vendor/ok"])
        # No declared modalities means a plain chat model, not an unusable one.
        self.assertTrue(models[0].is_text_chat)
        self.assertEqual(models[0].context_length, 65_536)
        # Missing pricing is unknown, not free.
        self.assertFalse(models[0].is_free)

    def test_non_object_payload_yields_nothing(self) -> None:
        self.assertEqual(catalog.parse_models(["nope"]), [])


if __name__ == "__main__":
    unittest.main()
