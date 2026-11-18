"""Custom questions go to the large tier; leading questions are flagged (VIS-LEADING-001)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.llm import ollama_client
from backend.vision import models as vision_models
from backend.vision.analyze import analyze
from backend.vision.questions import find_leading, leading_question_warning

# The question that produced a false pass in BrickWild (knowledge 40e2ae81):
# moondream answered "yes, a trestle table and a goblet" for images with neither.
BRICKWILD_LEADING = "is any European medieval item visible (trestle table, goblet...)?"
# The neutral rephrase that the large tier answered accurately.
BRICKWILD_NEUTRAL = "describe the floor and each piece of furniture"


class TierRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.describe = vision_models.get_purpose("describe")
        self.large = vision_models.REGISTRY[vision_models.LARGE].model_id
        self.small = vision_models.REGISTRY[vision_models.SMALL].model_id

    def test_custom_question_without_tier_resolves_large(self) -> None:
        route = vision_models.resolve_route(self.describe, question="What is on the table?")

        self.assertEqual(route.tier, vision_models.LARGE)
        self.assertEqual(route.model_id, self.large)
        self.assertIn("custom question", route.reason)
        self.assertIn("--tier small", route.reason, "the reason names the override")

    def test_explicit_small_tier_is_honoured_with_a_question(self) -> None:
        route = vision_models.resolve_route(self.describe, tier="small", question="What is on the table?")

        self.assertEqual(route.model_id, self.small)
        self.assertEqual(route.reason, "--tier small given")

    def test_explicit_model_is_honoured_with_a_question(self) -> None:
        route = vision_models.resolve_route(self.describe, override="llava:13b", question="What is this?")

        self.assertEqual(route.model_id, "llava:13b")
        self.assertEqual(route.tier, "")
        self.assertEqual(route.reason, "--model given")

    def test_default_describe_question_stays_small(self) -> None:
        for blank in ("", "   "):
            with self.subTest(question=blank):
                route = vision_models.resolve_route(self.describe, question=blank)
                self.assertEqual(route.model_id, self.small)
                self.assertIn("default", route.reason)

    def test_large_purposes_are_unchanged_by_a_question(self) -> None:
        route = vision_models.resolve_route(vision_models.get_purpose("ui"), question="Is the CTA obvious?")

        self.assertEqual(route.tier, vision_models.LARGE)
        self.assertIn("default", route.reason)

    def test_resolve_model_keeps_its_old_signature(self) -> None:
        self.assertEqual(vision_models.resolve_model(self.describe), self.small)
        self.assertEqual(vision_models.resolve_model(self.describe, override="x"), "x")


class LeadingQuestionDetectorTests(unittest.TestCase):
    def test_flags_the_brickwild_example(self) -> None:
        kinds = {finding.kind for finding in find_leading(BRICKWILD_LEADING)}

        self.assertIn("parenthesised list", kinds)

    def test_does_not_flag_the_neutral_rephrase(self) -> None:
        self.assertEqual(find_leading(BRICKWILD_NEUTRAL), [])
        self.assertIsNone(leading_question_warning(BRICKWILD_NEUTRAL))

    def test_flags_phrasings_that_name_candidates(self) -> None:
        for question in (
            "Is there a goblet on the table?",
            "Are there any torches on the wall?",
            "Can you see a sword?",
            "Does this page have a login form?",
            "What furniture is visible, such as beds or chests?",
            "Which objects are present, e.g. cups?",
            "Is it red or blue or green?",
            "Is it a table, a chair, or a bed?",
            "Which region is this: Greece, Japan or Norway?",
            "Which region does it resemble: South of France, Greek island, Scandinavia, Japan?",
            "What is the roof made of (thatch/tile)?",
        ):
            with self.subTest(question=question):
                self.assertTrue(find_leading(question), question)

    def test_leaves_neutral_questions_alone(self) -> None:
        for question in (
            "",
            "What colour is the background?",
            "Describe the floor, each piece of furniture, the lamps; which country's house does it resemble?",
            "What kind of shop is this? List the goods and fixtures you can see.",
            "This is a 3D render of a small village. Ignore that it is a render.",
            "Is the bed floating or on the floor?",          # one 'or' is a plain binary
            "Which is better, version 1 or version 2?",
            "Describe the roof, walls or doors.",            # aspects to describe, not answers
            "Review this interface. Cover: visual hierarchy, alignment and spacing, contrast.",
            "How many people are in the image, and what are they doing?",
        ):
            with self.subTest(question=question):
                self.assertEqual(find_leading(question), [], question)

    def test_warning_suggests_a_neutral_rephrase_and_classify(self) -> None:
        warning = leading_question_warning(BRICKWILD_LEADING)

        self.assertEqual(warning["code"], "leading-question")
        self.assertIn("(trestle table, goblet...)", warning["matches"])
        self.assertIn("neutral", warning["message"])
        self.assertIn("--purpose classify", warning["message"])

    def test_classify_warning_points_at_labels_instead(self) -> None:
        warning = leading_question_warning("Is it a table, a chair or a bed?", classify=True)

        self.assertIn("--labels", warning["message"])
        self.assertNotIn("--purpose classify", warning["message"])

    def test_huge_questions_are_checked_quickly(self) -> None:
        find_leading("a, " * 50_000 + "or b")  # must not hang on pathological input


class AnalyzeRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        from PIL import Image

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.image = str(Path(tmp.name) / "render.png")
        Image.new("RGB", (64, 64), (90, 70, 40)).save(self.image)

    def _analyze(self, **kwargs):
        with mock.patch.object(ollama_client, "has_model", return_value=True), \
                mock.patch.object(ollama_client, "chat", return_value="a wooden bed on the floor") as chat:
            result = analyze([self.image], **kwargs)
        return result, chat

    def test_question_goes_to_the_large_model_and_reports_why(self) -> None:
        result, chat = self._analyze(purpose="describe", question=BRICKWILD_NEUTRAL)

        large = vision_models.REGISTRY[vision_models.LARGE].model_id
        self.assertEqual(chat.call_args.kwargs["model"], large)
        self.assertEqual(result["model"], large)
        self.assertEqual(result["tier"], vision_models.LARGE)
        self.assertIn("custom question", result["tierReason"])
        self.assertEqual(result["warnings"], [])

    def test_small_tier_still_answers_when_asked_for(self) -> None:
        result, chat = self._analyze(purpose="describe", question=BRICKWILD_NEUTRAL, tier="small")

        self.assertEqual(chat.call_args.kwargs["model"], vision_models.REGISTRY[vision_models.SMALL].model_id)
        self.assertEqual(result["tierReason"], "--tier small given")

    def test_leading_question_is_answered_but_carries_a_warning(self) -> None:
        result, _ = self._analyze(purpose="describe", question=BRICKWILD_LEADING)

        self.assertEqual(result["answer"], "a wooden bed on the floor")
        self.assertEqual([w["code"] for w in result["warnings"]], ["leading-question"])

    def test_stock_describe_reports_its_default_route(self) -> None:
        result, _ = self._analyze(purpose="describe")

        self.assertEqual(result["tier"], vision_models.SMALL)
        self.assertEqual(result["tierReason"], "default for purpose 'describe'")


if __name__ == "__main__":
    unittest.main()
