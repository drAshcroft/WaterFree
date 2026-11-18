"""Spot questions that hand the vision model its answer.

A vision model asked "is any medieval item visible (trestle table, goblet...)?"
tends to find a trestle table and a goblet -- moondream did exactly that for
images containing neither. When the answer gates a review, a model that agrees
with the question is a false pass. This module flags the phrasings that name
candidate answers so the caller is warned and can ask neutrally.

It is a heuristic and only ever warns. Lists of *aspects to describe*
("describe the roof, walls or doors") are not candidate answers, so or-lists in
a clause that starts with an instruction to describe are left alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A question longer than this is not being typed by hand; checking a prefix is
# plenty and keeps the regexes' worst case bounded.
_MAX_CHARS = 2000

_WORD = r"[\w'-]+"
_ITEM = rf"{_WORD}(?:\s+{_WORD}){{0,2}}"   # a short noun phrase: 1-3 words

# (trestle table, goblet...) / (left or right) / (sword/axe) / (stone etc.)
_PAREN_LIST = re.compile(r"\(([^()]*?(?:,|\bor\b|/|\.\.\.|…|\betc\b)[^()]*)\)", re.IGNORECASE)
_SUCH_AS = re.compile(r"\bsuch as\b", re.IGNORECASE)
_EXAMPLE = re.compile(r"(?<![\w.])(?:e\.\s?g\.?|eg)(?![\w])|\bfor (?:example|instance)\b", re.IGNORECASE)
# red or blue or green
_OR_OR = re.compile(rf"\b{_WORD}\s+or\s+[^.?!;]*?\bor\s+{_WORD}", re.IGNORECASE)
# a table, a chair, or a bed / Greece, Japan or Norway. The list may not start
# right after a copula, so "which is better, version 1 or version 2" -- a
# two-way comparison -- is not read as the three candidates "better", ...
_QUESTION_WORD = r"(?:which|what|who|where|when|why|how|is|are|was|were|do|does|did|can|could|should|would|will)\b"
_COMMA_OR = re.compile(
    rf"\b(?<!\bis )(?<!\bare )(?<!\bwas )(?!{_QUESTION_WORD})"
    rf"(?:{_ITEM}\s*,\s*)+{_ITEM}\s*,?\s+or\s+{_ITEM}",
    re.IGNORECASE,
)
# "...does it resemble: South of France, Greek island, Scandinavia, Japan"
_COLON_LIST = re.compile(rf":\s*{_ITEM}(?:\s*,\s*{_ITEM}){{2,}}", re.IGNORECASE)
# is there a ... / are any ... / can you see a ... / does this page show a ...
_PRESENCE = re.compile(
    r"^(?:(?:is|are)\s+(?:there|any)\b"
    r"|(?:do|can|could)\s+you\s+(?:see|spot|find)\b"
    r"|does\s+(?:it|this(?:\s+\w+)?|the\s+\w+)\s+(?:show|contain|have|include)\b)",
    re.IGNORECASE,
)
# Clauses that ask for a description or a review: their lists name aspects to
# cover ("cover: hierarchy, spacing, contrast"), not candidate answers.
_DESCRIBE = re.compile(
    r"^(?:please\s+)?(?:describe|list|transcribe|explain|summari[sz]e|review|cover|check|inspect)\b",
    re.IGNORECASE,
)
# Split on ? ! ; and on a full stop that ends a sentence ("goblet...)" stays whole).
_CLAUSE_SPLIT = re.compile(r"[?!;]|\.(?=\s|$)")


@dataclass(frozen=True)
class Finding:
    kind: str     # short label for the phrasing
    text: str     # the span of the question that triggered it


def find_leading(question: str) -> list[Finding]:
    """Phrasings in `question` that name candidate answers, in order found."""
    text = (question or "").strip()[:_MAX_CHARS]
    if not text:
        return []
    findings: list[Finding] = []
    for match in _PAREN_LIST.finditer(text):
        findings.append(Finding("parenthesised list", match.group(0)))
    for match in _SUCH_AS.finditer(text):
        findings.append(Finding("'such as'", _context(text, match)))
    for match in _EXAMPLE.finditer(text):
        findings.append(Finding("example list", _context(text, match)))
    for clause in _clauses(text):
        if _PRESENCE.match(clause):
            findings.append(Finding("presence question", clause))
        if _DESCRIBE.match(clause):
            continue
        match = _COMMA_OR.search(clause) or _OR_OR.search(clause)
        if match:
            findings.append(Finding("either/or list", match.group(0)))
        elif match := _COLON_LIST.search(clause):
            findings.append(Finding("candidate list", match.group(0).lstrip(": ")))
    return findings


def leading_question_warning(question: str, *, classify: bool = False) -> dict | None:
    """A `warnings` entry for a leading question, or None for a neutral one."""
    findings = find_leading(question)
    if not findings:
        return None
    spans = "; ".join(f"{f.kind}: '{_clip(f.text)}'" for f in findings)
    if classify:
        advice = (
            "Put the candidates in --labels and keep the question neutral, e.g. "
            "'What does this image show?'."
        )
    else:
        advice = (
            "Ask neutrally and check the answer for the item yourself -- 'Describe "
            "each object on the table', not 'Is there a goblet?'. To pick from fixed "
            "options, use --purpose classify --labels a,b,c, which adds 'none' and "
            "'uncertain' and shuffles the order."
        )
    return {
        "code": "leading-question",
        "message": (
            f"The question names candidate answers ({spans}). Vision models, the "
            "small tier especially, tend to report what a question names whether or "
            f"not it is there. {advice}"
        ),
        "matches": [f.text for f in findings],
    }


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_SPLIT.split(text) if part.strip()]


def _context(text: str, match: re.Match, width: int = 40) -> str:
    return text[match.start(): match.end() + width].strip()


def _clip(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
