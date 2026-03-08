import os
import sys
import types
import unittest
from unittest.mock import patch


class _FakeAnthropicClient:
    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key


if "anthropic" not in sys.modules:
    sys.modules["anthropic"] = types.SimpleNamespace(
        Anthropic=_FakeAnthropicClient,
        types=types.SimpleNamespace(Message=object),
    )

from backend.llm.runtime_registry import (
    choose_runtime_for_stage,
    create_runtime,
    resolve_runtime_name,
)
from backend.llm.runtime_registry import list_runtime_descriptors


class RuntimeRegistryTests(unittest.TestCase):
    def test_resolve_defaults_to_anthropic(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_runtime_name(), "anthropic")

    def test_resolve_aliases_claude(self) -> None:
        self.assertEqual(resolve_runtime_name("claude"), "anthropic")

    def test_resolve_rejects_unknown_runtime(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runtime_name("unknown-runtime")

    def test_create_runtime_builds_anthropic_runtime(self) -> None:
        sentinel = object()
        with patch("backend.llm.runtime_registry.AnthropicRuntime", return_value=sentinel) as runtime_cls:
            runtime = create_runtime(runtime_name="anthropic")
        self.assertIs(runtime, sentinel)
        runtime_cls.assert_called_once()

    def test_create_runtime_rejects_unimplemented_runtime(self) -> None:
        with patch("backend.llm.runtime_registry.OllamaRuntime", return_value=object()) as runtime_cls:
            runtime = create_runtime(runtime_name="ollama")
        self.assertIsNotNone(runtime)
        runtime_cls.assert_called_once()

    def test_create_runtime_builds_deep_agents_runtime(self) -> None:
        sentinel = object()
        with patch("backend.llm.runtime_registry.DeepAgentsRuntime", return_value=sentinel) as runtime_cls:
            runtime = create_runtime(runtime_name="deep_agents", workspace_path="c:/repo")
        self.assertIs(runtime, sentinel)
        runtime_cls.assert_called_once()

    def test_create_runtime_builds_openai_runtime(self) -> None:
        sentinel = object()
        with patch("backend.llm.runtime_registry.OpenAIRuntime", return_value=sentinel) as runtime_cls:
            runtime = create_runtime(runtime_name="openai")
        self.assertIs(runtime, sentinel)
        runtime_cls.assert_called_once()

    def test_list_runtime_descriptors_exposes_all_lanes(self) -> None:
        ids = {item.id for item in list_runtime_descriptors()}
        self.assertIn("anthropic", ids)
        self.assertIn("deep_agents", ids)
        self.assertIn("ollama", ids)
        self.assertIn("openai", ids)

    def test_choose_runtime_for_stage_prefers_ollama_for_knowledge(self) -> None:
        selected = choose_runtime_for_stage(stage="knowledge", workload="knowledge extraction")
        self.assertEqual(selected, "ollama")


if __name__ == "__main__":
    unittest.main()
