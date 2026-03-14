from __future__ import annotations

import unittest

from backend.llm.channels.deepagents_channel import DeepAgentsChannel
from backend.llm.provider_factory import build_runtime_spec, extract_usage
from backend.llm.provider_profiles import (
    default_provider_profile_document,
    normalize_provider_profile,
)
from backend.llm.prompt_templates import build_system_prompt
from backend.llm.provider_resolver import resolve_provider
from backend.session.models import PlanDocument, RuntimeSelection
from backend.server import Server
from backend.test_support import make_temp_dir as make_test_dir


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
        self.assertEqual(profile.policies.persona_assignments[0].persona_id, "reviewer")
        self.assertEqual(profile.policies.persona_assignments[0].provider_id, "openai-primary")

    def test_resolve_provider_prefers_model_tier_routes_before_fallback(self) -> None:
        profile = normalize_provider_profile({
            "activeProviderId": "openai-primary",
            "catalog": [
                {
                    "id": "openai-primary",
                    "type": "openai",
                    "enabled": True,
                    "label": "OpenAI Primary",
                    "connection": {"style": "native", "baseUrl": "", "secretRef": "x", "apiKey": "sk"},
                    "models": {"default": "o3-mini"},
                },
                {
                    "id": "claude-fallback",
                    "type": "claude",
                    "enabled": True,
                    "label": "Claude Fallback",
                    "connection": {"style": "native", "baseUrl": "", "secretRef": "y", "apiKey": "ak"},
                    "models": {"default": "claude-sonnet-4-6"},
                },
            ],
            "policies": {
                "fallbackProviderOrder": ["claude-fallback", "openai-primary"],
                "modelTierRoutes": {
                    "smartest": {
                        "providerId": "openai-primary",
                        "model": "gpt-4o-mini",
                    }
                },
            },
        })

        planning = resolve_provider(profile, stage="PLANNING", persona="architect", preferred_runtime="deep_agents")

        self.assertIsNotNone(planning)
        self.assertEqual(planning.profile.id, "openai-primary")
        self.assertEqual(planning.model_name, "gpt-4o-mini")

    def test_resolve_provider_defaults_to_first_ordered_provider_when_unassigned(self) -> None:
        profile = normalize_provider_profile({
            "activeProviderId": "openai-primary",
            "catalog": [
                {
                    "id": "openai-primary",
                    "type": "openai",
                    "enabled": True,
                    "label": "OpenAI Primary",
                    "connection": {"style": "native", "baseUrl": "", "secretRef": "x", "apiKey": "sk"},
                    "models": {"default": "o3-mini"},
                },
                {
                    "id": "claude-cheap",
                    "type": "claude",
                    "enabled": True,
                    "label": "Claude Cheap",
                    "connection": {"style": "native", "baseUrl": "", "secretRef": "y", "apiKey": "ak"},
                    "models": {"default": "claude-sonnet-4-6"},
                },
            ],
            "policies": {
                "fallbackProviderOrder": ["claude-cheap", "openai-primary"],
            },
        })

        resolved = resolve_provider(profile, stage="QUESTION_ANSWER", persona="tutorializer", preferred_runtime="deep_agents")

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.profile.id, "claude-cheap")
        self.assertEqual(resolved.model_name, "claude-sonnet-4-6")

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

    def test_groq_default_profile_uses_groq_defaults(self) -> None:
        profile = default_provider_profile_document("groq")
        groq = profile.catalog[0]

        self.assertEqual(profile.active_provider_id, "groq-default")
        self.assertEqual(groq.id, "groq-default")
        self.assertEqual(groq.type, "groq")
        self.assertEqual(groq.label, "Groq")
        self.assertEqual(groq.model_for_stage("planning"), "llama-3.3-70b-versatile")
        self.assertEqual(groq.model_for_stage("annotation"), "llama-3.1-8b-instant")

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
            model_name=profile.catalog[0].model_for_stage("planning"),
        )

        self.assertIsInstance(agent, dict)
        self.assertEqual(usage_key, profile.catalog[0].id)
        self.assertEqual(agent["name"], profile.catalog[0].id)

    def test_server_sync_profile_invalidates_runtime_cache(self) -> None:
        workspace = str(make_test_dir(self, prefix="provider-profile-"))
        server = Server()
        self.addCleanup(server.close)

        first = server._get_runtime(workspace)
        profile = default_provider_profile_document("openai")
        server._set_provider_profile(workspace, profile)
        second = server._get_runtime(workspace)

        self.assertIsNot(first, second)

    def test_server_sync_profile_ignores_persona_prompt_overrides(self) -> None:
        workspace = str(make_test_dir(self, prefix="persona-prompt-"))
        server = Server()
        self.addCleanup(server.close)

        profile = normalize_provider_profile({
            "activeProviderId": "claude-primary",
            "catalog": [
                {
                    "id": "claude-primary",
                    "type": "claude",
                    "enabled": True,
                    "label": "Claude Primary",
                    "connection": {"style": "native", "baseUrl": "", "secretRef": "claude", "apiKey": "ak"},
                },
            ],
            "policies": {
                "personaPromptOverrides": {
                    "architect": "Custom architect prompt for this workspace.",
                },
            },
        })

        server._set_provider_profile(workspace, profile)
        prompt = build_system_prompt("PLANNING", "architect")

        self.assertNotIn("Custom architect prompt for this workspace.", prompt)
        self.assertIn("Translate the user's business goal into explicit technical requirements.", prompt)

    def test_session_runtime_selection_overrides_provider_and_model(self) -> None:
        workspace = str(make_test_dir(self, prefix="session-profile-"))
        server = Server()
        self.addCleanup(server.close)

        profile = normalize_provider_profile({
            "activeProviderId": "claude-primary",
            "catalog": [
                {
                    "id": "claude-primary",
                    "type": "claude",
                    "enabled": True,
                    "label": "Claude Primary",
                    "connection": {"style": "native", "baseUrl": "", "secretRef": "claude", "apiKey": "ak"},
                    "models": {"default": "claude-sonnet-4-6", "planning": "claude-sonnet-4-6"},
                },
                {
                    "id": "openai-primary",
                    "type": "openai",
                    "enabled": True,
                    "label": "OpenAI Primary",
                    "connection": {"style": "native", "baseUrl": "", "secretRef": "openai", "apiKey": "sk"},
                    "models": {"default": "o3-mini", "planning": "o3-mini"},
                },
            ],
        })
        server._set_provider_profile(workspace, profile)

        doc = PlanDocument(
            goal_statement="Fix the login flow",
            workspace_path=workspace,
            persona="architect",
            runtime_selection=RuntimeSelection(
                provider_id="openai-primary",
                model="gpt-4o-mini",
            ),
        )

        resolved = server._get_provider_profile_for_session(doc)
        active = next(item for item in resolved.catalog if item.id == resolved.active_provider_id)

        self.assertEqual(resolved.active_provider_id, "openai-primary")
        self.assertEqual(active.model_for_stage("planning"), "gpt-4o-mini")
        self.assertEqual(active.model_for_stage("execution"), "gpt-4o-mini")
