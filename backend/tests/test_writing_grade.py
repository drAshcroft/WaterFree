from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from backend.llm.chat_client import ChatTarget
from backend.test_support import make_temp_dir
from backend.writing_grade import core


def _grade_payload(*, score: int = 14, feedback: str = "The work is effective here.") -> str:
    return json.dumps({
        "genre": "short fiction",
        "dimensions": {
            key: {"score": score, "feedback": feedback}
            for key in core.DIMENSION_KEYS
        },
    })


def _target() -> ChatTarget:
    return ChatTarget(
        provider_type="openrouter",
        provider_label="OpenRouter",
        model="example/free:free",
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
    )


class GradeValidationTests(unittest.TestCase):
    def test_accepts_fenced_json_and_preserves_dimension_order(self) -> None:
        result = core._validate_grade(f"```json\n{_grade_payload()}\n```")

        self.assertEqual(result["genre"], "short fiction")
        self.assertEqual(tuple(result["dimensions"]), core.DIMENSION_KEYS)
        self.assertTrue(all(item["score"] == 14 for item in result["dimensions"].values()))

    def test_rejects_a_score_outside_the_public_scale(self) -> None:
        payload = json.loads(_grade_payload())
        payload["dimensions"]["interest"]["score"] = 21

        with self.assertRaises(core.GradeResponseError):
            core._validate_grade(json.dumps(payload))

    def test_repairs_multi_sentence_feedback_once(self) -> None:
        invalid = _grade_payload(feedback="This is good. It is memorable.")
        valid = _grade_payload(feedback="This is good and memorable.")

        with mock.patch.object(core, "_chat", side_effect=[invalid, valid]) as chat:
            result = core._render_grade("story", target=_target(), evidence_mode=False)

        self.assertEqual(chat.call_count, 2)
        self.assertEqual(
            result["dimensions"]["creativity"]["feedback"],
            "This is good and memorable.",
        )


class GradeRunTests(unittest.TestCase):
    def test_short_file_returns_computed_totals_and_metadata(self) -> None:
        workspace = make_temp_dir(self, prefix="writing-grade-")
        source = Path(workspace, "story.txt")
        source.write_text("A fox stole the moon and hid it in a teacup.", encoding="utf-8")
        target = _target()

        with (
            mock.patch.object(core, "resolve_chat_target", return_value=target) as resolve,
            mock.patch.object(core, "preflight") as preflight,
            mock.patch.object(core, "_chat", return_value=_grade_payload(score=15)),
        ):
            result = core.grade_writing(str(source), workspace_path=str(workspace))

        resolve.assert_called_once_with(
            stage="creative_writing",
            workspace_path=str(workspace),
            fallback_model=core._DEFAULT_MODEL,
        )
        preflight.assert_called_once_with(target)
        self.assertEqual(result["overall"], {"score": 90, "out_of": 120, "percentage": 75.0})
        self.assertEqual(result["evidence_mode"], "direct")
        self.assertEqual(result["chunks_processed"], 1)
        self.assertEqual(tuple(result["dimensions"]), core.DIMENSION_KEYS)

    def test_long_file_uses_ordered_map_reduce_evidence(self) -> None:
        workspace = make_temp_dir(self, prefix="writing-grade-long-")
        source = Path(workspace, "novel.txt")
        source.write_text("First scene.\n\nSecond scene.\n\nThird scene.", encoding="utf-8")

        with (
            mock.patch.object(core, "_CHUNK_SIZE_CHARS", 15),
            mock.patch.object(core, "resolve_chat_target", return_value=_target()),
            mock.patch.object(core, "preflight"),
            mock.patch.object(core, "_analyze_chunks", return_value=["note one", "note two", "note three"]),
            mock.patch.object(core, "_render_grade", return_value=core._validate_grade(_grade_payload())) as render,
        ):
            result = core.grade_writing(str(source), workspace_path=str(workspace))

        self.assertEqual(result["evidence_mode"], "map_reduce")
        self.assertEqual(result["chunks_processed"], 3)
        evidence = render.call_args.args[0]
        self.assertLess(evidence.index("note one"), evidence.index("note two"))
        self.assertLess(evidence.index("note two"), evidence.index("note three"))


if __name__ == "__main__":
    unittest.main()
