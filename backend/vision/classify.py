"""Pick one label for an image, as JSON a script can check.

Built for blind identity checks ("does this render read as a Greek island or
as Japan?"), where free text has to be parsed and a model that agrees with the
question produces a false pass. So:

- 'none' and 'uncertain' are always allowed. Without an exit, a model forced
  to choose picks the nearest label, and a gate reads that as a pass.
- The caller's labels are shuffled per call and the order is recorded: models
  lean toward the first option offered, and a fixed order turns that lean into
  a systematic bias across a whole batch of renders.
- The schema asks for evidence before the label, so the model states what it
  sees before committing to an answer rather than justifying one afterwards.
- The reply is constrained to the schema where Ollama supports it and is
  validated regardless: an out-of-set label gets one stricter retry, then
  becomes 'uncertain' with the raw answer kept for inspection.

Pure functions only -- the model call is injected -- so this module has no
dependency on Ollama and the validation rules are unit-testable on their own.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable
from dataclasses import dataclass

NONE = "none"
UNCERTAIN = "uncertain"
RESERVED: tuple[str, ...] = (NONE, UNCERTAIN)
CONFIDENCE: tuple[str, ...] = ("low", "medium", "high")
MIN_LABELS = 2
MAX_LABELS = 20
MAX_LABEL_CHARS = 80

_rng = random.Random()


@dataclass(frozen=True)
class Verdict:
    label: str
    evidence: str
    confidence: str
    note: str = ""      # a non-fatal oddity worth a warning


def parse_labels(raw: str | list[str] | None) -> list[str]:
    """The caller's labels: trimmed, de-duplicated, reserved ones dropped.

    Raises ValueError (a usage error) unless 2-20 distinct labels remain.
    'none' and 'uncertain' do not count towards that: they are always added,
    and a single real label is a yes/no question in disguise -- exactly the
    leading shape this purpose exists to avoid.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ValueError(
            f"--purpose classify needs --labels a,b,c ({MIN_LABELS}-{MAX_LABELS} "
            "comma-separated labels; 'none' and 'uncertain' are added for you)."
        )
    parts = raw.split(",") if isinstance(raw, str) else [str(part) for part in raw]
    labels: list[str] = []
    seen: set[str] = set()
    for part in parts:
        label = " ".join(part.split())
        key = label.casefold()
        if not label or key in seen or key in RESERVED:
            continue
        if len(label) > MAX_LABEL_CHARS:
            raise ValueError(f"Label '{label[:40]}...' is over {MAX_LABEL_CHARS} characters.")
        seen.add(key)
        labels.append(label)
    if not MIN_LABELS <= len(labels) <= MAX_LABELS:
        raise ValueError(
            f"--labels needs {MIN_LABELS}-{MAX_LABELS} distinct labels besides 'none' "
            f"and 'uncertain'; got {len(labels)}."
        )
    return labels


def presentation_order(labels: list[str], rng: random.Random | None = None) -> list[str]:
    """`labels` shuffled, then the two escape hatches last, always in the same place."""
    shuffled = list(labels)
    (rng or _rng).shuffle(shuffled)
    return shuffled + list(RESERVED)


def response_schema(order: list[str]) -> dict:
    """Ollama structured-output schema. Property order is generation order."""
    return {
        "type": "object",
        "properties": {
            "evidence": {"type": "string"},
            "label": {"type": "string", "enum": list(order)},
            "confidence": {"type": "string", "enum": list(CONFIDENCE)},
        },
        "required": ["evidence", "label", "confidence"],
    }


def build_prompt(question: str, order: list[str], *, retry_reason: str = "", default_question: str = "") -> str:
    ask = question.strip() or default_question or "Which one label best describes what this image shows?"
    listing = "\n".join(f"- {label}" for label in order)
    lines = []
    if retry_reason:
        lines += [
            f"Your previous reply could not be used: {retry_reason}.",
            (
                "Copy the label exactly from the list below. Reply with the JSON "
                "object only -- no prose, no code fences."
            ),
            "",
        ]
    lines += [
        ask,
        "",
        "Allowed labels, in no particular order:",
        listing,
        "",
        f"Use '{NONE}' if no label fits, and '{UNCERTAIN}' if the image does not let you decide.",
        "Reply with a JSON object:",
        (
            '{"evidence": "<one sentence naming the visible cues>", '
            '"label": "<one label from the list, copied exactly>", '
            '"confidence": "low" | "medium" | "high"}'
        ),
    ]
    return "\n".join(lines)


def parse_answer(raw: str, allowed: list[str]) -> tuple[Verdict | None, str]:
    """(verdict, "") for a usable reply, else (None, why it is unusable)."""
    data = _extract_json(raw)
    if not isinstance(data, dict):
        return None, "the reply was not a JSON object"
    label_raw = data.get("label")
    if not isinstance(label_raw, str) or not label_raw.strip():
        return None, "the reply had no label"
    label = _match_label(label_raw, allowed)
    if label is None:
        return None, f"'{label_raw.strip()[:60]}' is not one of the allowed labels"
    evidence = data.get("evidence")
    evidence = " ".join(evidence.split()) if isinstance(evidence, str) else ""
    confidence = str(data.get("confidence") or "").strip().lower()
    note = ""
    if confidence not in CONFIDENCE:
        note = f"confidence {data.get('confidence')!r} is not low/medium/high; reported as 'low'"
        confidence = "low"
    return Verdict(label=label, evidence=evidence, confidence=confidence, note=note), ""


def classify(
    generate: Callable[[str], str],
    *,
    order: list[str],
    question: str = "",
    default_question: str = "",
) -> dict:
    """Ask, validate, retry once with a stricter instruction, else 'uncertain'.

    `generate(prompt)` returns the model's raw reply. The returned dict has
    label/evidence/confidence, `attempts`, `answer` (the last raw reply) and
    `warnings`.
    """
    raw = generate(build_prompt(question, order, default_question=default_question))
    verdict, problem = parse_answer(raw, order)
    attempts = 1
    first_problem = problem
    if verdict is None:
        raw = generate(build_prompt(
            question, order, retry_reason=problem, default_question=default_question,
        ))
        attempts = 2
        verdict, problem = parse_answer(raw, order)

    warnings: list[dict] = []
    if verdict is None:
        warnings.append({
            "code": "classify-unusable-answer",
            "message": (
                f"The model's reply was unusable twice ({first_problem}; then {problem}), "
                f"so the label is '{UNCERTAIN}'. Its last reply is in `answer`."
            ),
        })
        verdict = Verdict(label=UNCERTAIN, evidence="", confidence="low")
    elif verdict.note:
        warnings.append({"code": "classify-confidence-normalised", "message": verdict.note})
    return {
        "label": verdict.label,
        "evidence": verdict.evidence,
        "confidence": verdict.confidence,
        "attempts": attempts,
        "answer": raw.strip(),
        "warnings": warnings,
    }


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _extract_json(raw: str) -> object:
    """The JSON value in a reply: bare, fenced, or embedded in surrounding prose."""
    text = (raw or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start: end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _match_label(label: str, allowed: list[str]) -> str | None:
    """The allowed label `label` names, tolerating case, spacing and quoting."""
    wanted = " ".join(label.strip().strip("\"'`.").split()).casefold()
    for candidate in allowed:
        if candidate.casefold() == wanted:
            return candidate
    return None
