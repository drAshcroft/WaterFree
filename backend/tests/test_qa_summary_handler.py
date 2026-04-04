import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.handlers.qa_summary_handler import handle_run_qa_summary


class QaSummaryHandlerTests(unittest.TestCase):
    def test_handle_run_qa_summary_resolves_workspace_relative_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            expected_source = str((workspace / "README.md").resolve())
            payload = json.dumps({
                "source": expected_source,
                "question": "What changed?",
                "response": "Summary response",
            })

            with mock.patch(
                "backend.handlers.qa_summary_handler._qa_summary_impl",
                return_value=payload,
            ) as qa_summary_mock:
                result = handle_run_qa_summary(
                    None,
                    {
                        "workspacePath": str(workspace),
                        "fileOrUrl": "README.md",
                        "question": "What changed?",
                    },
                )

            qa_summary_mock.assert_called_once_with(expected_source, "What changed?")
            self.assertEqual(result["source"], expected_source)
            self.assertEqual(result["response"], "Summary response")

    def test_handle_run_qa_summary_leaves_urls_untouched(self) -> None:
        payload = json.dumps({
            "source": "https://example.com/doc",
            "question": "Summarize this",
            "response": "Remote response",
        })

        with mock.patch(
            "backend.handlers.qa_summary_handler._qa_summary_impl",
            return_value=payload,
        ) as qa_summary_mock:
            result = handle_run_qa_summary(
                None,
                {
                    "workspacePath": "C:/Projects/WaterFree",
                    "fileOrUrl": "https://example.com/doc",
                    "question": "Summarize this",
                },
            )

        qa_summary_mock.assert_called_once_with("https://example.com/doc", "Summarize this")
        self.assertEqual(result["response"], "Remote response")


if __name__ == "__main__":
    unittest.main()
