"""Credential preservation and visible failures at provider/agent boundaries."""
import importlib
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.handlers.runtime_handler import handle_sync_provider_profile
from backend.llm.channels.deepagents_channel import DeepAgentsChannel
from backend.llm.provider_factory import ProviderConstructionError, build_runtime_spec
from backend.llm.provider_profiles import normalize_provider_profile, provider_to_dict
from backend.server import Server
from backend.test_support import make_temp_dir


def _document():
    return normalize_provider_profile({
        "activeProviderId": "router",
        "catalog": [{
            "id": "router", "type": "openrouter", "label": "Router", "enabled": True,
            "connection": {"apiKey": "test-credential", "baseUrl": "https://example.invalid/v1"},
            "models": {"default": "vendor/model", "planning": "vendor/model"},
        }],
    })


class ProviderReliabilityTests(unittest.TestCase):
    def test_synced_credentials_survive_storage_and_model_construction(self):
        from langchain_openai import ChatOpenAI

        workspace = str(make_temp_dir(self, prefix="credential-flow-"))
        server = Server()
        self.addCleanup(server.close)
        result = handle_sync_provider_profile(server, {
            "workspacePath": workspace, "profile": _document().to_dict(),
        })
        self.assertTrue(result["ok"])
        document = server._get_provider_profile(workspace)
        self.assertEqual(document.catalog[0].connection.api_key, "test-credential")
        spec = build_runtime_spec(
            document.catalog[0], runtime_name="deep_agents", stage="PLANNING",
            persona="default", session_key="test", policies=document.policies,
        )
        self.assertIsInstance(spec.model, ChatOpenAI)
        self.assertEqual(spec.model.openai_api_key.get_secret_value(), "test-credential")

    def test_both_connection_key_spellings_round_trip(self):
        for spelling in ("api_key", "apiKey"):
            raw = provider_to_dict(_document().catalog[0])
            raw["connection"].pop("api_key", None)
            raw["connection"][spelling] = "round-trip-key"
            with self.subTest(spelling=spelling):
                result = normalize_provider_profile({"catalog": [raw]})
                self.assertEqual(result.catalog[0].connection.api_key, "round-trip-key")
                self.assertEqual(result.catalog[0].connection.base_url, "https://example.invalid/v1")

    def test_adapter_failures_log_the_original_cause_without_model_strings(self):
        adapters = (
            ("anthropic", "AnthropicAdapter", "langchain_anthropic.ChatAnthropic"),
            ("openai", "OpenAIAdapter", "langchain_openai.ChatOpenAI"),
            ("groq", "GroqAdapter", "langchain_groq.ChatGroq"),
            ("gemini", "GeminiAdapter", "langchain_google_genai.ChatGoogleGenerativeAI"),
            ("qwen", "QwenAdapter", "langchain_openai.ChatOpenAI"),
            ("openrouter", "OpenRouterAdapter", "langchain_openai.ChatOpenAI"),
        )
        for provider, name, constructor in adapters:
            module = importlib.import_module(f"backend.llm.adapters.{provider}_adapter")
            package, attribute = constructor.rsplit(".", 1)
            fake_package = SimpleNamespace(**{
                attribute: mock.Mock(side_effect=ValueError("credential rejected"))
            })
            with self.subTest(provider=provider), mock.patch.dict(
                "sys.modules", {package: fake_package}
            ), self.assertLogs(module.log, level="WARNING") as logs:
                model = getattr(module, name)().create_llm("vendor/model", {"api_key": "test"})
                self.assertIsNone(model)
                self.assertIn("credential rejected", logs.output[0])
                self.assertIn("vendor/model", logs.output[0])

    def test_factory_rejects_an_unavailable_provider_client(self):
        document = _document()
        with mock.patch("backend.llm.provider_factory.get_adapter") as get_adapter:
            get_adapter.return_value.create_llm.return_value = None
            with self.assertRaisesRegex(ProviderConstructionError, "Router.*openrouter"):
                build_runtime_spec(
                    document.catalog[0], runtime_name="deep_agents", stage="PLANNING",
                    persona="default", session_key="test", policies=document.policies,
                )

    def test_agent_construction_error_reaches_the_caller_and_is_not_cached(self):
        document = _document()
        skills = mock.Mock()
        skills.augment_system_prompt.side_effect = lambda prompt, bundle: prompt
        channel = DeepAgentsChannel(
            provider_lane="deep_agents", provider_profile_document=document,
            deepagents_factory=mock.Mock(side_effect=ValueError("unsupported tool schema")),
            filesystem_backend_factory=None, skill_adapter=skills,
            build_system_prompt_fn=lambda *args: "system", build_tools_fn=lambda *args: [],
            subagents_fn=lambda: [], interrupt_config_fn=lambda: {},
        )
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "router.*vendor/model.*unsupported tool schema"):
                channel.run(stage="PLANNING", prompt="build", persona="default", workspace_path=".")
        self.assertEqual(channel._da_factory.call_count, 2)
        self.assertEqual(channel._agents, {})
