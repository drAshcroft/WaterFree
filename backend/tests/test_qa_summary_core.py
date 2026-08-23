import unittest
from unittest import mock

from backend.llm.chat_client import ChatTarget
from backend.qa_summary import core


def _local_target(model: str = "freehuntx/qwen3-coder:14b") -> ChatTarget:
    return ChatTarget(
        provider_type="ollama",
        provider_label="Ollama (local)",
        model=model,
        base_url="http://localhost:11434",
        api_key="",
    )


class QaSummaryCoreTests(unittest.TestCase):
    def test_chat_helper_forwards_target_and_token_budget(self) -> None:
        target = _local_target()
        with mock.patch("backend.qa_summary.core.chat", return_value="OK") as chat:
            result = core._chat(target, "system", "user", max_tokens=12)  # noqa: SLF001

        self.assertEqual(result, "OK")
        chat.assert_called_once()
        messages = chat.call_args.args[0]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(chat.call_args.kwargs["target"], target)
        self.assertEqual(chat.call_args.kwargs["max_tokens"], 12)

    def test_final_answer_prompt_is_direct_by_default(self) -> None:
        with mock.patch(
            "backend.qa_summary.core._chat",
            return_value="WaterFree is a VS Code extension.",
        ) as chat:
            answer = core._render_final_answer(  # noqa: SLF001
                "WaterFree is a VS Code extension for structured AI pair programming.",
                target=_local_target(),
                question="What is this project?",
                file_or_url="README.md",
            )

        self.assertEqual(answer, "WaterFree is a VS Code extension.")
        prompt = "\n".join(str(part) for part in chat.call_args.args[1:])
        self.assertIn("Answer the question directly", prompt)
        self.assertNotIn("Supporting Details", prompt)
        self.assertEqual(chat.call_args.kwargs["max_tokens"], 256)

    def test_detailed_questions_get_a_larger_final_budget(self) -> None:
        with mock.patch("backend.qa_summary.core._chat", return_value="Long answer") as chat:
            core._render_final_answer(  # noqa: SLF001
                "notes",
                target=_local_target(),
                question="Explain this in detail",
                file_or_url="README.md",
            )

        self.assertEqual(chat.call_args.kwargs["max_tokens"], 1024)

    def test_run_reports_the_resolved_provider_and_model(self) -> None:
        target = ChatTarget(
            provider_type="openrouter",
            provider_label="OpenRouter",
            model="qwen/qwen3-coder",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test",
        )
        with (
            mock.patch("backend.qa_summary.core.resolve_chat_target", return_value=target),
            mock.patch("backend.qa_summary.core.preflight") as preflight,
            mock.patch("backend.qa_summary.core._read_source_text", return_value="hello world"),
            mock.patch("backend.qa_summary.core._chat", return_value="answer"),
        ):
            result = core.run_qa_summary("README.md", "What is this?", workspace_path="/ws")

        # Preflight runs once for the whole run, not once per chunk.
        preflight.assert_called_once_with(target)
        self.assertEqual(result["provider"], "openrouter")
        self.assertEqual(result["model"], "qwen/qwen3-coder")
        self.assertEqual(result["response"], "answer")


if __name__ == "__main__":
    unittest.main()
