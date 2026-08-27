import unittest
from unittest import mock

from backend.llm.chat_client import ChatTarget, ChatUnavailable
from backend.testing import summary
from backend.testing.results import TestResult as _Result
from backend.testing.results import TestRunResult as _RunResult

_TARGET = ChatTarget(
    provider_type="openrouter", provider_label="R", model="v/free:free",
    base_url="https://openrouter.ai/api/v1", api_key="sk-or-test",
)


def _run(failed: int, passed: int = 3) -> _RunResult:
    results = [_Result(name=f"test_ok_{i}", passed=True) for i in range(passed)]
    results += [
        _Result(name=f"test_bad_{i}", passed=False, error="AssertionError: boom")
        for i in range(failed)
    ]
    return _RunResult(passed=passed, failed=failed, results=results, raw_output="raw")


class SummarizeRunTests(unittest.TestCase):
    def test_a_green_run_costs_no_model_call(self) -> None:
        with mock.patch.object(summary, "chat") as chat:
            text = summary.summarize_run(_run(failed=0))

        chat.assert_not_called()
        self.assertIn("passed", text)

    def test_failures_are_mapped_in_batches_then_reduced(self) -> None:
        run = _run(failed=13)  # 3 batches of <= 6, plus one reduce call

        with mock.patch.object(summary, "resolve_chat_target", return_value=_TARGET), \
                mock.patch.object(summary, "preflight"), \
                mock.patch.object(summary, "chat", return_value="note") as chat:
            summary.summarize_run(run, workspace_path="")

        self.assertEqual(chat.call_count, 4)

    def test_long_error_text_is_truncated_head_and_tail(self) -> None:
        result = _Result(name="t", passed=False, error="A" * 5000 + "TAIL_MARKER")

        rendered = summary._render_failure(result)

        self.assertLess(len(rendered), 2000)
        self.assertIn("TAIL_MARKER", rendered)
        self.assertIn("[truncated]", rendered)

    def test_provider_failure_propagates(self) -> None:
        with mock.patch.object(summary, "resolve_chat_target", return_value=_TARGET), \
                mock.patch.object(summary, "preflight",
                                  side_effect=ChatUnavailable("no key")):
            with self.assertRaises(ChatUnavailable):
                summary.summarize_run(_run(failed=1))


class SummarizeLogTests(unittest.TestCase):
    def test_empty_log_is_a_usage_error(self) -> None:
        with self.assertRaises(ValueError):
            summary.summarize_log("   ")

    def test_chunks_reporting_no_failures_are_dropped(self) -> None:
        with mock.patch.object(summary, "resolve_chat_target", return_value=_TARGET), \
                mock.patch.object(summary, "preflight"), \
                mock.patch.object(summary, "chat", return_value="NONE"):
            text = summary.summarize_log("some output")

        self.assertIn("No failures", text)

    def test_an_oversized_log_keeps_only_head_and_tail(self) -> None:
        huge = "x" * (summary._LOG_CHUNK_CHARS * 40)

        with mock.patch.object(summary, "resolve_chat_target", return_value=_TARGET), \
                mock.patch.object(summary, "preflight"), \
                mock.patch.object(summary, "chat", return_value="note") as chat:
            summary.summarize_log(huge)

        # _LOG_MAX_CHUNKS map calls plus one reduce.
        self.assertEqual(chat.call_count, summary._LOG_MAX_CHUNKS + 1)


if __name__ == "__main__":
    unittest.main()
