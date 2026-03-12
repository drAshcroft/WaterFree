from __future__ import annotations

import shutil
import tempfile
import unittest

from backend.llm.channels.deepagents_channel import DeepAgentsChannel
from backend.llm.provider_factory import build_runtime_spec, extract_usage
from backend.llm.provider_profiles import (
    default_provider_profile_document,
    normalize_provider_profile,
)
from backend.llm.provider_resolver import resolve_provider
from backend.server import Server


class _DummySkillAdapter:
    def select(self, **kwargs):
        class _Bundle:
            preferred_tool_categories: list[str] = []

        return _Bundle()

    def augment_system_prompt(self, prompt: str, bundle) -> str:
        _ = bundle
        return prompt


class ProviderProfileTests(unittest.TestCase):
    def test_normalize_profile_defaults_and_preserves_active_provider(self) -> None:
        profile = normalize_provider_profile({
            "activeProviderId": "openai-primary",
            "catalog": [
                {
                    "id": "openai-primary",
                    "type": "openai",
                    "enabled": True,
                    "label": "OpenAI Primary",
                    "connection": {
                        "style": "native",
                        "baseUrl": "",
                        "secretRef": "waterfree.provider.openai-primary.key",
                        "apiKey": "sk-test",
                    },
                    "models": {"default": "o3-mini", "execution": "gpt-4o"},
                    "routing": {"useForStages": ["planning", "execution"], "personas": ["reviewer"]},
                },
                {
                    "id": "claude-secondary",
                    "type": "claude",
                    "enabled": True,
                    "label": "Claude Secondary",
                    "connection": {
                        "style": "native",
                        "baseUrl": "",
                        "secretRef": "waterfree.provider.claude-secondary.key",
                        "apiKey": "sk-ant",
                    },
                },
            ],
            "policies": {"fallbackProviderOrder": ["claude-secondary", "openai-primary"]},
        })

        self.assertEqual(profile.active_provider_id, "openai-primary")
        self.assertEqual(profile.policies.fallback_provider_order[0], "claude-secondary")
        self.assertEqual(profile.catalog[0].models["execution"], "gpt-4o")

    def test_resolve_provider_honors_stage_persona_and_fallback(self) -> None:
        profile = normalize_provider_profile({
            "activeProviderId": "openai-primary",
            "catalog": [
                {
                    "id": "openai-primary",
                    "type": "openai",
                    "enabled": True,
                    "label": "OpenAI Primary",
                    "connection": {"style": "native", "baseUrl": "", "secretRef": "x", "apiKey": "sk"},
                    "routing": {"useForStages": ["planning"], "personas": ["reviewer"]},
                },
                {
                    "id": "claude-fallback",
                    "type": "claude",
                    "enabled": True,
                    "label": "Claude Fallback",
                    "connection": {"style": "native", "baseUrl": "", "secretRef": "y", "apiKey": "ak"},
                },
            ],
            "policies": {"fallbackProviderOrder": ["claude-fallback", "openai-primary"]},
        })

        planning = resolve_provider(profile, stage="PLANNING", persona="reviewer", preferred_runtime="deep_agents")
        execution = resolve_provider(profile, stage="EXECUTION", persona="coding_agent", preferred_runtime="deep_agents")

        self.assertIsNotNone(planning)
        self.assertEqual(planning.profile.id, "openai-primary")
        self.assertIsNotNone(execution)
        self.assertEqual(execution.profile.id, "claude-fallback")

    def test_openai_runtime_spec_preserves_cache_metadata_and_usage_extraction(self) -> None:
        profile = default_provider_profile_document("openai").catalog[0]
        spec = build_runtime_spec(
            profile,
            runtime_name="openai",
            stage="PLANNING",
            persona="reviewer",
            session_key="session-1",
            policies=default_provider_profile_document("openai").policies,
        )
        usage = extract_usage({
            "messages": [
                {
                    "response_metadata": {
                        "usage": {
                            "prompt_tokens": 120,
                            "completion_tokens": 40,
                            "prompt_tokens_details": {"cached_tokens": 80},
                        }
                    }
                }
            ]
        }, "openai")

        self.assertTrue(spec.metadata["useResponsesApi"])
        self.assertTrue(spec.metadata["usePreviousResponseId"])
        self.assertIn("prompt_cache_key", spec.metadata["model_kwargs"])
        self.assertEqual(usage["cache_read_tokens"], 80)
        self.assertEqual(usage["cache_creation_tokens"], 0)

    def test_channel_create_agent_handles_current_summarization_api(self) -> None:
        profile = default_provider_profile_document("claude")
        channel = DeepAgentsChannel(
            provider_lane="deep_agents",
            provider_profile_document=profile,
            deepagents_factory=lambda **kwargs: kwargs,
            filesystem_backend_factory=lambda **kwargs: object(),
            skill_adapter=_DummySkillAdapter(),
            build_system_prompt_fn=lambda stage, persona: f"{stage}:{persona}",
            build_tools_fn=lambda workspace_path, persona, stage, bundle: [],
            subagents_fn=lambda: [],
            interrupt_config_fn=lambda: {},
        )

        agent, usage_key = channel._create_agent(
            stage="PLANNING",
            persona="default",
            workspace_path="c:/repo",
            session_key="session-1",
            provider_profile=profile.catalog[0],
            runtime_name="deep_agents",
        )

        self.assertIsInstance(agent, dict)
        self.assertEqual(usage_key, profile.catalog[0].id)
        self.assertEqual(agent["name"], profile.catalog[0].id)

    def test_server_sync_profile_invalidates_runtime_cache(self) -> None:
        workspace = tempfile.mkdtemp(prefix="waterfree-provider-profile-")
        self.addCleanup(lambda: shutil.rmtree(workspace, ignore_errors=True))
        server = Server()
        self.addCleanup(server.close)

        first = server._get_runtime(workspace)
        profile = default_provider_profile_document("openai")
        server._set_provider_profile(workspace, profile)
        second = server._get_runtime(workspace)

        self.assertIsNot(first, second)
