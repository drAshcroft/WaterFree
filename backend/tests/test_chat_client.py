import json
import unittest
from unittest import mock

from backend.llm import chat_client
from backend.llm.chat_client import ChatTarget, ChatUnavailable
from backend.llm.provider_profiles import normalize_provider_profile


def _profile(*, stages: list[str], provider_type: str = "openrouter", api_key: str = "") -> object:
    return normalize_provider_profile({
        "activeProviderId": "router",
        "catalog": [{
            "id": "router",
            "type": provider_type,
            "enabled": True,
            "label": "My Router",
            "connection": {"apiKey": api_key},
            # Pinned per stage: reader stages otherwise default to the
            # `auto:free` sentinel, which resolves against the live catalog.
            "models": {"default": "qwen/qwen3-coder", "qa_summary": "qwen/qwen3-coder"},
            "routing": {"useForStages": stages},
        }],
        "policies": {"fallbackProviderOrder": ["router"]},
    })


class ResolveChatTargetTests(unittest.TestCase):
    def test_reader_stage_is_opt_in(self) -> None:
        """A provider serving the agent stages must NOT capture qa_summary.

        This is the guarantee that stops an existing workspace's bulk
        summarization from silently moving onto a paid provider.
        """
        document = _profile(stages=["planning", "annotation", "execution"])

        target = chat_client.resolve_chat_target(
            stage="qa_summary",
            document=document,
            fallback_model="local-model",
        )

        self.assertEqual(target.provider_type, "ollama")
        self.assertEqual(target.model, "local-model")

    def test_naming_the_stage_claims_it(self) -> None:
        document = _profile(stages=["planning", "qa_summary"], api_key="sk-or-inline")

        target = chat_client.resolve_chat_target(stage="qa_summary", document=document)

        self.assertEqual(target.provider_type, "openrouter")
        self.assertEqual(target.model, "qwen/qwen3-coder")
        self.assertEqual(target.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(target.api_key, "sk-or-inline")

    def test_key_falls_back_to_environment_when_profile_has_none(self) -> None:
        """The CLI path: providers.json on disk never carries a key."""
        document = _profile(stages=["qa_summary"], api_key="")

        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-or-env"}):
            target = chat_client.resolve_chat_target(stage="qa_summary", document=document)

        self.assertEqual(target.api_key, "sk-or-env")

    def test_no_document_means_local(self) -> None:
        target = chat_client.resolve_chat_target(stage="qa_summary", fallback_model="local-model")
        self.assertEqual(target.provider_type, "ollama")


class PreflightTests(unittest.TestCase):
    def test_missing_remote_key_names_the_env_var(self) -> None:
        target = ChatTarget("openrouter", "My Router", "qwen/qwen3-coder", "https://x", "")

        with self.assertRaises(ChatUnavailable) as ctx:
            chat_client.preflight(target)

        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))

    def test_remote_key_present_passes_without_touching_ollama(self) -> None:
        target = ChatTarget("openrouter", "My Router", "qwen/qwen3-coder", "https://x", "sk-or")
        with mock.patch.object(chat_client.ollama_client, "ensure_daemon") as daemon:
            chat_client.preflight(target)
        daemon.assert_not_called()


class OpenAiCompatibleTransportTests(unittest.TestCase):
    TARGET = ChatTarget(
        "openrouter", "My Router", "qwen/qwen3-coder", "https://openrouter.ai/api/v1", "sk-or",
    )

    def _urlopen(self, payload: dict):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__.return_value = response
        return mock.patch("urllib.request.urlopen", return_value=response)

    def test_request_shape(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.data)
            response = mock.MagicMock()
            response.read.return_value = json.dumps(
                {"choices": [{"message": {"content": " hi "}}]}
            ).encode()
            response.__enter__.return_value = response
            return response

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            answer = chat_client.chat(
                [{"role": "user", "content": "hello"}],
                target=self.TARGET,
                max_tokens=64,
            )

        self.assertEqual(answer, "hi")
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/chat/completions")
        # urllib title-cases header names.
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-or")
        self.assertEqual(captured["body"]["model"], "qwen/qwen3-coder")
        # The caller's budget maps onto max_tokens, not Ollama's num_predict.
        self.assertEqual(captured["body"]["max_tokens"], 64)
        # Ollama-only fields must never reach an OpenAI-compatible gateway.
        self.assertNotIn("options", captured["body"])
        self.assertNotIn("keep_alive", captured["body"])
        self.assertNotIn("think", captured["body"])

    def test_error_body_with_http_200_is_surfaced(self) -> None:
        """OpenRouter reports upstream vendor failures in a 200 body."""
        with self._urlopen({"error": {"message": "upstream is down"}}):
            with self.assertRaises(ChatUnavailable) as ctx:
                chat_client.chat([{"role": "user", "content": "x"}], target=self.TARGET, max_tokens=8)

        self.assertIn("upstream is down", str(ctx.exception))

    def test_empty_choices_names_the_model(self) -> None:
        with self._urlopen({"choices": []}):
            with self.assertRaises(ChatUnavailable) as ctx:
                chat_client.chat([{"role": "user", "content": "x"}], target=self.TARGET, max_tokens=8)

        self.assertIn("qwen/qwen3-coder", str(ctx.exception))


class LocalTransportTests(unittest.TestCase):
    TARGET = ChatTarget("ollama", "Ollama", "llama3.2", "http://localhost:11434", "")

    def test_budget_maps_to_num_predict(self) -> None:
        with mock.patch.object(chat_client.ollama_client, "chat", return_value=" OK ") as chat:
            result = chat_client.chat(
                [{"role": "user", "content": "x"}], target=self.TARGET, max_tokens=12,
            )

        self.assertEqual(result, "OK")
        self.assertEqual(chat.call_args.kwargs["options"], {"num_predict": 12})
        self.assertEqual(chat.call_args.kwargs["keep_alive"], "30m")

    def test_ollama_error_is_normalized(self) -> None:
        """Callers should only ever have to catch ChatUnavailable."""
        error = chat_client.ollama_client.OllamaError("daemon down")
        with mock.patch.object(chat_client.ollama_client, "chat", side_effect=error):
            with self.assertRaises(ChatUnavailable) as ctx:
                chat_client.chat([{"role": "user", "content": "x"}], target=self.TARGET, max_tokens=8)

        self.assertIn("daemon down", str(ctx.exception))


class UnsupportedProviderTests(unittest.TestCase):
    def test_anthropic_reader_routing_is_rejected_clearly(self) -> None:
        target = ChatTarget("claude", "Claude", "claude-sonnet-4-6", "https://api.anthropic.com", "k")

        with self.assertRaises(ChatUnavailable) as ctx:
            chat_client.chat([{"role": "user", "content": "x"}], target=target, max_tokens=8)

        self.assertIn("OpenRouter", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
