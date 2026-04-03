from __future__ import annotations

import json
import unittest
from unittest import mock

from backend.mcp_qa_summary import (
    _qa_summary_impl,
    _reduce_chunk_notes,
    _split_into_chunks,
)


class McpQaSummaryTests(unittest.TestCase):
    def test_split_into_chunks_breaks_large_text(self) -> None:
        text = ("A paragraph with useful content.\n" * 4000).strip()
        chunks = _split_into_chunks(text, max_chars=1200)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1200 for chunk in chunks))
        self.assertTrue(chunks[0].startswith("A paragraph"))

    def test_reduce_chunk_notes_runs_hierarchical_merge(self) -> None:
        notes = [f"note {i}" for i in range(13)]

        with mock.patch("backend.mcp_qa_summary._merge_note_batch") as merge_mock:
            merge_mock.side_effect = (
                lambda batch, question, round_index, batch_index: (
                    f"merge:r{round_index}:b{batch_index}:n{len(batch)}"
                )
            )
            merged = _reduce_chunk_notes(notes, "What changed?")

        self.assertEqual(merge_mock.call_count, 3)
        self.assertEqual(merged, "merge:r2:b1:n3")

    def test_qa_summary_impl_returns_metadata_and_response(self) -> None:
        source_text = "Line one.\nLine two.\n" * 1500

        with mock.patch("backend.mcp_qa_summary._ensure_ollama_ready"), mock.patch(
            "backend.mcp_qa_summary._read_source_text",
            return_value=source_text,
        ), mock.patch(
            "backend.mcp_qa_summary._analyze_chunk",
            side_effect=["chunk-a", "chunk-b", "chunk-c"],
        ), mock.patch(
            "backend.mcp_qa_summary._reduce_chunk_notes",
            return_value="merged-notes",
        ), mock.patch(
            "backend.mcp_qa_summary._render_final_answer",
            return_value="final-detailed-answer",
        ):
            payload = json.loads(
                _qa_summary_impl("README.md", "Summarize key points and risks.")
            )

        self.assertEqual(payload["source"], "README.md")
        self.assertEqual(payload["question"], "Summarize key points and risks.")
        self.assertEqual(payload["chunks_processed"], 3)
        self.assertEqual(payload["response"], "final-detailed-answer")
        self.assertGreater(payload["source_characters"], 1000)


if __name__ == "__main__":
    unittest.main()
