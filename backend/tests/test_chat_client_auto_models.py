"""Coverage for the `auto:free` / `auto:floor` fallback chain."""

import unittest
from dataclasses import replace
from unittest import mock

from backend.llm import chat_client, openrouter_catalog
from backend.llm.chat_client import ChatTarget, ChatUnavailable
from backend.llm.provider_profiles import normalize_provider_profile


def _profile(model: str, *, stages: list[str], api_key: str = "sk-or-test") -> object:
    # Pinned on every stage under test: a bare "default" loses to the
    # per-stage entries in DEFAULT_STAGE_MODELS.
    models = {"default": model}
    models.update({stage: model for stage in stages})
    return normalize_provider_profile({
        "activeProviderId": "router",
        "catalog": [{
            "id": "router",
            "type": "openrouter",
            "enabled": True,
            "label": "My Router",
            "connection": {"apiKey": api_key},
            "models": models,
            "routing": {"useForStages": stages},
        }],
        "policies": {"fallbackProviderOrder": ["router"]},
    })


_MODELS = openrouter_catalog.parse_models({"data": [
    {"id": "v/free-a:free", "context_length": 128_000,
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "v/free-b:free", "context_length": 64_000,
     "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "v/cheap", "context_length": 64_000,
     "pricing": {"prompt": "0.0000001", "completion": "0.0000002"}},
]})


class ExpansionTests(unittest.TestCase):
    def test_auto_free_expands_into_a_chain_ending_at_local(self) -> None:
        document = _profile("auto:free", stages=["qa_summary"])

        with mock.patch.object(openrouter_catalog, "load_models", return_value=_MODELS):
            target = chat_client.resolve_chat_target(
                stage="qa_summary", document=document, fallback_model="local-model",
            )

        chain = target.chain()
        self.assertEqual(chain[0].model, "v/free-a:free")
        self.assertEqual(chain[1].model, "v/free-b:free")
        self.assertEqual(chain[-1].provider_type, "ollama")
        self.assertEqual(chain[-1].model, "local-model")

    def test_auto_floor_skips_free_models(self) -> None:
        document = _profile("auto:floor", stages=["testing"])

        with mock.patch.object(openrouter_catalog, "load_models", return_value=_MODELS):
            target = chat_client.resolve_chat_target(
                stage="testing", document=document, fallback_model="local-model",
            )

        self.assertEqual(target.model, "v/cheap")

    def test_catalog_outage_falls_back_to_local(self) -> None:
        document = _profile("auto:free", stages=["qa_summary"])
        outage = openrouter_catalog.CatalogUnavailable("offline")

        with mock.patch.object(openrouter_catalog, "load_models", side_effect=outage):
            target = chat_client.resolve_chat_target(
                stage="qa_summary", document=document, fallback_model="local-model",
            )

        self.assertEqual(target.provider_type, "ollama")

    def test_sentinel_on_a_non_openrouter_provider_is_rejected(self) -> None:
        document = normalize_provider_profile({
            "activeProviderId": "oai",
            "catalog": [{
                "id": "oai", "type": "openai", "enabled": True, "label": "OpenAI",
                "connection": {"apiKey": "sk-test"},
                "models": {"default": "auto:free", "qa_summary": "auto:free"},
                "routing": {"useForStages": ["qa_summary"]},
            }],
            "policies": {"fallbackProviderOrder": ["oai"]},
        })

        with self.assertRaises(ChatUnavailable):
            chat_client.resolve_chat_target(stage="qa_summary", document=document)

    def test_a_concrete_model_id_is_left_alone(self) -> None:
        document = _profile("qwen/qwen3-coder", stages=["qa_summary"])

        with mock.patch.object(openrouter_catalog, "load_models") as load:
            target = chat_client.resolve_chat_target(stage="qa_summary", document=document)

        load.assert_not_called()
        self.assertEqual(target.model, "qwen/qwen3-coder")
        self.assertEqual(target.alternates, ())


def _remote(model: str) -> ChatTarget:
    return ChatTarget(
        provider_type="openrouter", provider_label="R", model=model,
        base_url="https://openrouter.ai/api/v1", api_key="sk-or-test",
    )


class ChainWalkTests(unittest.TestCase):
    def test_chat_falls_through_to_the_next_candidate(self) -> None:
        target = _remote("v/free-a:free")
        target = replace(target, alternates=(_remote("v/cheap"),))
        calls: list[str] = []

        def fake(messages, *, target, max_tokens, timeout):
            calls.append(target.model)
            if target.model == "v/free-a:free":
                raise ChatUnavailable("429 rate limited")
            return "ok"

        with mock.patch.object(chat_client, "_chat_once", side_effect=fake):
            answer = chat_client.chat([], target=target, max_tokens=10)

        self.assertEqual(answer, "ok")
        self.assertEqual(calls, ["v/free-a:free", "v/cheap"])

    def test_every_candidate_failing_reports_all_of_them(self) -> None:
        target = _remote("v/free-a:free")
        target = replace(target, alternates=(_remote("v/cheap"),))

        with mock.patch.object(chat_client, "_chat_once",
                               side_effect=ChatUnavailable("down")):
            with self.assertRaises(ChatUnavailable) as ctx:
                chat_client.chat([], target=target, max_tokens=10)

        self.assertIn("v/free-a:free", str(ctx.exception))
        self.assertIn("v/cheap", str(ctx.exception))

    def test_preflight_passes_when_any_link_is_usable(self) -> None:
        keyless = ChatTarget(
            provider_type="openrouter", provider_label="R", model="v/free-a:free",
            base_url="https://openrouter.ai/api/v1", api_key="",
            alternates=(_remote("v/cheap"),),
        )

        chat_client.preflight(keyless)  # must not raise

    def test_preflight_reports_every_failure_when_none_are_usable(self) -> None:
        keyless = ChatTarget(
            provider_type="openrouter", provider_label="R", model="v/free-a:free",
            base_url="https://openrouter.ai/api/v1", api_key="",
        )

        with self.assertRaises(ChatUnavailable) as ctx:
            chat_client.preflight(keyless)

        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
