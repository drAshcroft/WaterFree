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

from backend.llm.runtime_registry import create_runtime, resolve_runtime_name


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
        with self.assertRaises(NotImplementedError):
            create_runtime(runtime_name="ollama")


if __name__ == "__main__":
    unittest.main()
