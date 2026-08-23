import threading
import time
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


def _remote_target(model: str = "anthropic/claude-sonnet-5") -> ChatTarget:
    return ChatTarget(
        provider_type="openrouter",
        provider_label="OpenRouter",
        model=model,
        base_url="https://openrouter.ai/api/v1",
        api_key="key",
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

    def test_local_target_analyzes_chunks_serially(self) -> None:
        """Ollama serves one request at a time per model; concurrency buys nothing."""
        chunks = [f"chunk-{i}" for i in range(5)]
        concurrent = 0
        peak = 0

        def fake_chat(target, system_prompt, user_prompt, *, max_tokens):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            concurrent -= 1
            return user_prompt

        with mock.patch("backend.qa_summary.core._chat", side_effect=fake_chat):
            notes = core._analyze_chunks(  # noqa: SLF001
                chunks, target=_local_target(), question="q"
            )

        self.assertEqual(peak, 1)
        self.assertEqual(len(notes), 5)

    def test_remote_target_preserves_source_order_under_concurrency(self) -> None:
        """
        The reduction tree reads notes positionally, so out-of-order results would
        silently mis-attribute facts. Slower early chunks must still land first.
        """
        chunks = [f"chunk-{i}" for i in range(8)]

        def fake_analyze(chunk, *, target, chunk_index, chunk_total, question):
            # Invert the delay so early chunks finish last if order is not enforced.
            time.sleep((chunk_total - chunk_index) * 0.01)
            return f"note-for-{chunk}"

        with mock.patch("backend.qa_summary.core._analyze_chunk", side_effect=fake_analyze):
            notes = core._analyze_chunks(  # noqa: SLF001
                chunks, target=_remote_target(), question="q"
            )

        self.assertEqual(notes, [f"note-for-chunk-{i}" for i in range(8)])

    def test_remote_target_runs_chunks_concurrently(self) -> None:
        chunks = [f"chunk-{i}" for i in range(6)]
        barrier_hits = []
        lock = threading.Lock()
        live = 0
        peak = 0

        def fake_analyze(chunk, *, target, chunk_index, chunk_total, question):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
                barrier_hits.append(chunk_index)
            time.sleep(0.05)
            with lock:
                live -= 1
            return chunk

        with mock.patch("backend.qa_summary.core._analyze_chunk", side_effect=fake_analyze):
            core._analyze_chunks(chunks, target=_remote_target(), question="q")  # noqa: SLF001

        self.assertGreater(peak, 1, "remote analysis should overlap requests")
        self.assertLessEqual(peak, core._REMOTE_ANALYSIS_WORKERS)  # noqa: SLF001

    def test_single_chunk_skips_the_pool(self) -> None:
        with mock.patch("backend.qa_summary.core._analyze_chunk", return_value="only") as analyze:
            notes = core._analyze_chunks(["one"], target=_remote_target(), question="q")  # noqa: SLF001

        self.assertEqual(notes, ["only"])
        self.assertEqual(analyze.call_args.kwargs["chunk_total"], 1)

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
