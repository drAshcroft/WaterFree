import unittest
from unittest import mock

from backend.qa_summary import core


class QaSummaryCoreTests(unittest.TestCase):
    def test_ollama_chat_keeps_model_warm_and_caps_generation(self) -> None:
        with mock.patch("backend.qa_summary.core.ollama_client.chat", return_value=" OK ") as chat:
            result = core._ollama_chat(  # noqa: SLF001
                [{"role": "user", "content": "Say OK"}],
                num_predict=12,
            )

        self.assertEqual(result, "OK")
        chat.assert_called_once()
        kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs["keep_alive"], "30m")
        self.assertEqual(kwargs["options"], {"num_predict": 12})

    def test_final_answer_prompt_is_direct_by_default(self) -> None:
        with mock.patch("backend.qa_summary.core._ollama_chat", return_value="WaterFree is a VS Code extension.") as chat:
            answer = core._render_final_answer(  # noqa: SLF001
                "WaterFree is a VS Code extension for structured AI pair programming.",
                question="What is this project?",
                file_or_url="README.md",
            )

        self.assertEqual(answer, "WaterFree is a VS Code extension.")
        messages = chat.call_args.args[0]
        prompt = "\n".join(message["content"] for message in messages)
        self.assertIn("Answer the question directly", prompt)
        self.assertNotIn("Supporting Details", prompt)
        self.assertEqual(chat.call_args.kwargs["num_predict"], 256)

    def test_detailed_questions_get_a_larger_final_budget(self) -> None:
        with mock.patch("backend.qa_summary.core._ollama_chat", return_value="Long answer") as chat:
            core._render_final_answer(  # noqa: SLF001
                "notes",
                question="Explain this in detail",
                file_or_url="README.md",
            )

        self.assertEqual(chat.call_args.kwargs["num_predict"], 1024)


if __name__ == "__main__":
    unittest.main()
