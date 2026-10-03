import asyncio
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from backend.llm.auto_agent import AutoFallbackChatModel, build_auto_agent_model
from backend.llm.chat_client import ChatTarget
from backend.llm.openrouter_catalog import parse_models
from backend.llm.provider_profiles import normalize_provider_profile


def target(provider, model):
    return ChatTarget(provider, provider, model, "http://example.invalid", "")


class AutoAgentTests(unittest.TestCase):
    def test_fallback_runs_the_tool_once_and_preserves_the_followup(self):
        from langchain.agents import create_agent
        from langchain_core.tools import tool
        events = []

        @tool
        def record(value: str) -> str:
            """Record a test event."""
            events.append(value)
            return "saved"

        remote = mock.Mock()
        remote.bind_tools.return_value = remote
        remote.invoke.side_effect = RuntimeError("429 rate limited")
        local = mock.Mock()
        local.bind_tools.return_value = local
        local.invoke.side_effect = lambda messages, **kw: (
            AIMessage(content="finished") if isinstance(messages[-1], ToolMessage)
            else AIMessage(content="", tool_calls=[{"name": "record", "args": {"value": "once"}, "id": "call-1", "type": "tool_call"}])
        )
        model = AutoFallbackChatModel(candidates=[(target("openrouter", "v/free"), remote), (target("ollama", "local"), local)])
        agent = create_agent(model=model, tools=[record])
        result = agent.invoke({"messages": [{"role": "user", "content": "Record once"}]})
        self.assertEqual(events, ["once"])
        self.assertEqual(result["messages"][-1].content, "finished")
        self.assertEqual(len(model.trace.snapshot()), 2)
        self.assertTrue(all(row["model"] == "local" for row in model.trace.snapshot()))
        self.assertTrue(local.bind_tools.called)

    def test_async_fallback_preserves_tool_calls_and_usage(self):
        remote, local = mock.Mock(), mock.Mock()
        remote.ainvoke = mock.AsyncMock(side_effect=RuntimeError("429"))
        message = AIMessage(content="", tool_calls=[{"name": "test", "args": {}, "id": "1", "type": "tool_call"}],
                            usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
        local.ainvoke = mock.AsyncMock(return_value=message)
        model = AutoFallbackChatModel(candidates=[(target("openrouter", "v/free"), remote), (target("ollama", "local"), local)])
        result = asyncio.run(model.ainvoke("test"))
        self.assertEqual(result.tool_calls, message.tool_calls)
        self.assertEqual(result.usage_metadata["total_tokens"], 10)

    def test_partial_stream_is_never_replayed_on_another_model(self):
        def partial(*args, **kwargs):
            yield AIMessageChunk(content="started")
            raise RuntimeError("connection lost")
        first, second = mock.Mock(), mock.Mock()
        first.stream.side_effect = partial
        model = AutoFallbackChatModel(candidates=[(target("openrouter", "v/first"), first), (target("ollama", "local"), second)])
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            list(model.stream("test"))
        second.stream.assert_not_called()

    def test_empty_failed_stream_falls_back(self):
        first, second = mock.Mock(), mock.Mock()
        first.stream.side_effect = RuntimeError("429")
        second.stream.return_value = iter([AIMessageChunk(content="ok")])
        model = AutoFallbackChatModel(candidates=[(target("openrouter", "v/free"), first), (target("ollama", "local"), second)])
        self.assertEqual("".join(chunk.content for chunk in model.stream("test")), "ok")

    def test_exhaustion_is_visible_and_harness_gate_is_skipped_on_next_call(self):
        first, second = mock.Mock(), mock.Mock()
        first.invoke.side_effect = RuntimeError("Gate Free Endpoints by Agentic Harness")
        second.invoke.side_effect = RuntimeError("local unavailable")
        model = AutoFallbackChatModel(candidates=[(target("openrouter", "v/free"), first), (target("ollama", "local"), second)])
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "No agent model"):
                model.invoke("test")
        self.assertEqual(first.invoke.call_count, 1)

    def test_factory_filters_non_tool_models_and_never_passes_a_sentinel_to_sdk(self):
        profile = normalize_provider_profile({"catalog": [{"id": "router", "type": "openrouter",
            "connection": {"apiKey": "test"}, "models": {"default": "auto:free"}}]}).catalog[0]
        catalog = parse_models({"data": [
            {"id": "v/no-tools", "context_length": 128000, "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "v/tools", "context_length": 64000, "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        ]})
        with mock.patch("backend.llm.openrouter_catalog.load_models", return_value=catalog), \
             mock.patch("backend.llm.adapters.openrouter_adapter.OpenRouterAdapter.create_llm", return_value=mock.Mock()) as create, \
             mock.patch("langchain_ollama.ChatOllama", return_value=mock.Mock()):
            model = build_auto_agent_model(profile, "auto:free")
        self.assertEqual(create.call_args.args[0], "v/tools")
        self.assertEqual([item.model for item, _ in model.candidates], ["v/tools", "freehuntx/qwen3-coder:14b"])


if __name__ == "__main__":
    unittest.main()
