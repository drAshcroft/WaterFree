"""`--purpose classify --labels a,b,c` -> {label, evidence, confidence} (VIS-CLASSIFY-001).

The model is always stubbed: these tests pin the contract a gate script relies
on (validated label, bounded retry, 'uncertain' fallback), not model quality.
"""

import argparse
import contextlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.cli import vision as cli_vision
from backend.llm import ollama_client
from backend.vision import classify as cls
from backend.vision import models as vision_models
from backend.vision import web
from backend.vision.analyze import analyze

LABELS = "South of France, Greek island, Scandinavia, Japan"


def _reply(label: str, evidence: str = "whitewashed cubic houses, blue domes", confidence: str = "high") -> str:
    return json.dumps({"evidence": evidence, "label": label, "confidence": confidence})


class ParseLabelsTests(unittest.TestCase):
    def test_comma_list_is_trimmed_and_deduplicated(self) -> None:
        self.assertEqual(
            cls.parse_labels(" cat ,dog,  Cat, big   dog ,,"),
            ["cat", "dog", "big dog"],
        )

    def test_missing_labels_is_a_usage_error_that_names_the_flag(self) -> None:
        for raw in (None, "", "   "):
            with self.subTest(raw=raw), self.assertRaises(ValueError) as ctx:
                cls.parse_labels(raw)
            self.assertIn("--labels", str(ctx.exception))

    def test_reserved_labels_are_dropped_and_do_not_count(self) -> None:
        self.assertEqual(cls.parse_labels("cat,dog,None,UNCERTAIN"), ["cat", "dog"])
        with self.assertRaises(ValueError):
            cls.parse_labels("cat,none,uncertain")  # one real label is a yes/no question

    def test_label_count_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            cls.parse_labels("only-one")
        self.assertEqual(len(cls.parse_labels(",".join(f"l{i}" for i in range(20)))), 20)
        with self.assertRaises(ValueError):
            cls.parse_labels(",".join(f"l{i}" for i in range(21)))

    def test_absurdly_long_label_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            cls.parse_labels("cat," + "x" * (cls.MAX_LABEL_CHARS + 1))


class PresentationOrderTests(unittest.TestCase):
    def test_labels_are_shuffled_and_the_escape_hatches_come_last(self) -> None:
        labels = [f"label{i}" for i in range(8)]
        orders = {tuple(cls.presentation_order(labels, random.Random(seed))) for seed in range(10)}

        self.assertGreater(len(orders), 1, "order must vary between calls")
        for order in orders:
            self.assertEqual(sorted(order[:-2]), sorted(labels))
            self.assertEqual(order[-2:], ("none", "uncertain"))

    def test_schema_constrains_label_and_confidence_and_asks_for_evidence_first(self) -> None:
        schema = cls.response_schema(["b", "a", "none", "uncertain"])

        self.assertEqual(list(schema["properties"]), ["evidence", "label", "confidence"])
        self.assertEqual(schema["properties"]["label"]["enum"], ["b", "a", "none", "uncertain"])
        self.assertEqual(schema["properties"]["confidence"]["enum"], ["low", "medium", "high"])


class ParseAnswerTests(unittest.TestCase):
    ALLOWED = ("Greek island", "Japan", "none", "uncertain")

    def test_valid_json_is_returned_as_is(self) -> None:
        verdict, problem = cls.parse_answer(_reply("Greek island"), self.ALLOWED)

        self.assertEqual(problem, "")
        self.assertEqual(
            (verdict.label, verdict.evidence, verdict.confidence),
            ("Greek island", "whitewashed cubic houses, blue domes", "high"),
        )

    def test_case_spacing_and_quotes_resolve_to_the_canonical_label(self) -> None:
        verdict, _ = cls.parse_answer(_reply("  'greek   ISLAND.' "), self.ALLOWED)

        self.assertEqual(verdict.label, "Greek island")

    def test_fenced_or_embedded_json_is_accepted(self) -> None:
        for raw in (f"```json\n{_reply('Japan')}\n```", f"Here you go: {_reply('Japan')} Hope that helps."):
            with self.subTest(raw=raw):
                verdict, _ = cls.parse_answer(raw, self.ALLOWED)
                self.assertEqual(verdict.label, "Japan")

    def test_unusable_replies_say_why(self) -> None:
        cases = {
            _reply("Mexico"): "not one of the allowed labels",
            "It looks Greek to me.": "not a JSON object",
            json.dumps({"evidence": "x"}): "no label",
            json.dumps(["Japan"]): "not a JSON object",
        }
        for raw, reason in cases.items():
            with self.subTest(raw=raw):
                verdict, problem = cls.parse_answer(raw, self.ALLOWED)
                self.assertIsNone(verdict)
                self.assertIn(reason, problem)

    def test_bad_confidence_is_normalised_to_low_with_a_note(self) -> None:
        verdict, _ = cls.parse_answer(_reply("Japan", confidence="very sure"), self.ALLOWED)

        self.assertEqual(verdict.confidence, "low")
        self.assertIn("very sure", verdict.note)


class ClassifyFlowTests(unittest.TestCase):
    ORDER = ("Japan", "Greek island", "none", "uncertain")

    def test_valid_answer_takes_one_attempt(self) -> None:
        generate = mock.Mock(return_value=_reply("Greek island"))

        out = cls.classify(generate, order=self.ORDER)

        self.assertEqual(generate.call_count, 1)
        self.assertEqual(out["label"], "Greek island")
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["attempts"], 1)
        self.assertEqual(out["warnings"], [])

    def test_out_of_set_label_is_retried_once_with_a_stricter_prompt(self) -> None:
        generate = mock.Mock(side_effect=[_reply("Mexico"), _reply("Japan", confidence="medium")])

        out = cls.classify(generate, order=self.ORDER)

        self.assertEqual(out["label"], "Japan")
        self.assertEqual(out["attempts"], 2)
        retry_prompt = generate.call_args_list[1].args[0]
        self.assertIn("'Mexico' is not one of the allowed labels", retry_prompt)
        self.assertIn("copied exactly", retry_prompt)

    def test_two_bad_answers_become_uncertain_with_the_raw_answer(self) -> None:
        generate = mock.Mock(side_effect=[_reply("Mexico"), "Probably Mexico, honestly."])

        out = cls.classify(generate, order=self.ORDER)

        self.assertEqual(generate.call_count, 2, "exactly one retry")
        self.assertEqual(out["label"], "uncertain")
        self.assertEqual(out["confidence"], "low")
        self.assertEqual(out["answer"], "Probably Mexico, honestly.")
        self.assertEqual([w["code"] for w in out["warnings"]], ["classify-unusable-answer"])

    def test_prompt_lists_labels_in_the_presented_order(self) -> None:
        prompt = cls.build_prompt("", self.ORDER)

        positions = [prompt.index(f"- {label}") for label in self.ORDER]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("in no particular order", prompt)


def _image(directory: str) -> str:
    from PIL import Image

    path = Path(directory) / "village.png"
    Image.new("RGB", (64, 64), (240, 240, 255)).save(path)
    return str(path)


class AnalyzeClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.image = _image(tmp.name)
        self.dir = tmp.name

    def test_result_is_machine_checkable_and_advisory(self) -> None:
        with mock.patch.object(ollama_client, "has_model", return_value=True), \
                mock.patch.object(ollama_client, "chat", return_value=_reply("Greek island")) as chat:
            result = analyze([self.image], purpose="classify", labels=LABELS)

        self.assertEqual(result["label"], "Greek island")
        self.assertEqual(result["evidence"], "whitewashed cubic houses, blue domes")
        self.assertEqual(result["confidence"], "high")
        self.assertTrue(result["advisory"])
        self.assertEqual(result["labels"],
                         ["South of France", "Greek island", "Scandinavia", "Japan", "none", "uncertain"])
        self.assertEqual(sorted(result["labelOrder"]), sorted(result["labels"]))
        self.assertEqual(result["model"], vision_models.REGISTRY[vision_models.LARGE].model_id)

        kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs["response_format"]["properties"]["label"]["enum"], result["labelOrder"])
        self.assertEqual(kwargs["options"]["temperature"], 0)
        prompt = kwargs["messages"][-1]["content"]
        positions = [prompt.index(f"- {label}") for label in result["labelOrder"]]
        self.assertEqual(positions, sorted(positions), "labelOrder records what the model saw")

    def test_framing_is_for_any_image_not_a_web_page(self) -> None:
        system = vision_models.get_purpose("classify").system.lower()

        self.assertIn("render", system)
        self.assertIn("sprite", system)
        self.assertNotIn("web page", system)
        self.assertNotIn("front-end", system)

    def test_out_of_set_twice_through_analyze(self) -> None:
        with mock.patch.object(ollama_client, "has_model", return_value=True), \
                mock.patch.object(ollama_client, "chat", side_effect=[_reply("Peru"), _reply("Peru")]) as chat:
            result = analyze([self.image], purpose="classify", labels=LABELS)

        self.assertEqual(chat.call_count, 2)
        self.assertEqual(result["label"], "uncertain")
        self.assertEqual(result["attempts"], 2)
        self.assertIn("Peru", result["answer"])
        self.assertIn("classify-unusable-answer", [w["code"] for w in result["warnings"]])

    def test_usage_errors_are_raised_before_ollama_is_contacted(self) -> None:
        cases = [
            {"images": [self.image], "purpose": "classify", "labels": None},
            {"images": [self.image], "purpose": "classify", "labels": "just-one"},
            {"images": [self.image, self.image], "purpose": "classify", "labels": LABELS},
            {"images": [self.image], "purpose": "describe", "labels": LABELS},
        ]
        for case in cases:
            with self.subTest(case=case), \
                    mock.patch.object(ollama_client, "has_model") as has_model, \
                    mock.patch.object(ollama_client, "chat") as chat:
                with self.assertRaises(ValueError):
                    analyze(case.pop("images"), **case)
                has_model.assert_not_called()
                chat.assert_not_called()

    def test_web_rejects_missing_labels_before_launching_a_browser(self) -> None:
        with mock.patch.object(web, "capture_web") as capture, self.assertRaises(ValueError):
            web.analyze_web("https://example.com", purpose="classify")
        capture.assert_not_called()


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    parser = argparse.ArgumentParser(prog="waterfree")
    cli_vision.register(parser.add_subparsers(dest="area"))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            args = parser.parse_args(argv)
            code = args._runner(args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class ClassifyCliTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.image = _image(tmp.name)

    def test_missing_labels_is_a_usage_error(self) -> None:
        with mock.patch.object(ollama_client, "chat") as chat:
            code, out, err = _run_cli(["vision", "look", self.image, "--purpose", "classify"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--labels", err)
        chat.assert_not_called()

    def test_purposes_lists_classify_on_the_large_tier(self) -> None:
        code, out, _ = _run_cli(["vision", "purposes"])

        rows = {row["purpose"]: row for row in json.loads(out)["purposes"]}
        self.assertEqual(code, 0)
        self.assertIn("classify", rows)
        self.assertEqual(rows["classify"]["tier"], "large")
        self.assertIn("--labels", rows["classify"]["summary"])

    def test_look_classify_emits_the_verdict(self) -> None:
        with mock.patch.object(ollama_client, "has_model", return_value=True), \
                mock.patch.object(ollama_client, "chat", return_value=_reply("Japan", confidence="low")):
            code, out, _ = _run_cli(["vision", "look", self.image, "--purpose", "classify",
                                     "--labels", LABELS])

        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((payload["label"], payload["confidence"]), ("Japan", "low"))
        self.assertTrue(payload["advisory"])


if __name__ == "__main__":
    unittest.main()
