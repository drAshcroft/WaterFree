import os
import unittest
from unittest.mock import patch

from backend.llm.runtime_registry import (
    choose_runtime_for_stage,
    create_runtime,
    resolve_runtime_name,
)
from backend.llm.runtime_registry import list_runtime_descriptors


class RuntimeRegistryTests(unittest.TestCase):
    def test_resolve_defaults_to_deep_agents(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_runtime_name(), "deep_agents")

    def test_resolve_aliases_claude_to_deep_agents(self) -> None:
        self.assertEqual(resolve_runtime_name("claude"), "deep_agents")

    def test_resolve_aliases_anthropic_to_deep_agents(self) -> None:
        self.assertEqual(resolve_runtime_name("anthropic"), "deep_agents")

    def test_resolve_rejects_unknown_runtime(self) -> None:
        with self.assertRaises(ValueError):
            resolve_runtime_name("unknown-runtime")

    def test_create_runtime_routes_ollama_lane_to_deep_agents(self) -> None:
        with patch("backend.llm.runtime_registry.DeepAgentsRuntime", return_value=object()) as runtime_cls:
            runtime = create_runtime(runtime_name="ollama")
        self.assertIsNotNone(runtime)
        runtime_cls.assert_called_once()

    def test_create_runtime_builds_deep_agents_runtime(self) -> None:
        sentinel = object()
        with patch("backend.llm.runtime_registry.DeepAgentsRuntime", return_value=sentinel) as runtime_cls:
            runtime = create_runtime(runtime_name="deep_agents", workspace_path="c:/repo")
        self.assertIs(runtime, sentinel)
        runtime_cls.assert_called_once()

    def test_create_runtime_routes_openai_lane_to_deep_agents(self) -> None:
        sentinel = object()
        with patch("backend.llm.runtime_registry.DeepAgentsRuntime", return_value=sentinel) as runtime_cls:
            runtime = create_runtime(runtime_name="openai")
        self.assertIs(runtime, sentinel)
        runtime_cls.assert_called_once()

    def test_list_runtime_descriptors_exposes_all_lanes(self) -> None:
        ids = {item.id for item in list_runtime_descriptors()}
        self.assertIn("deep_agents", ids)
        self.assertIn("ollama", ids)
        self.assertIn("openai", ids)

    def test_choose_runtime_for_stage_prefers_ollama_for_knowledge(self) -> None:
        selected = choose_runtime_for_stage(stage="knowledge", workload="knowledge extraction")
        self.assertEqual(selected, "ollama")


if __name__ == "__main__":
    unittest.main()
