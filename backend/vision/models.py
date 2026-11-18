"""Which local vision model serves which purpose.

Two tiers, deliberately. A single model is either too slow for bulk work or too
weak for real analysis, and three tiers means three multi-gigabyte downloads for
a marginal quality step. So:

    small   moondream        ~1.7 GB   the stock describe prompt, bulk passes
    large   qwen2.5vl:7b     ~6.0 GB   UI critique, triage, reading text, function,
                                       classify, and any caller-written question

The small tier answers questions; it does not classify. moondream returns an
empty string for yes/no prompts and for anything demanding a verdict token
("Is this correct?", "answer OK or BROKEN"), while answering open
"describe / what is / what problems" questions reliably. Purposes that need a
structured judgement therefore sit on the large tier regardless of how cheap
the job sounds.

Nothing is ever downloaded implicitly. A missing model raises with the exact
`waterfree vision pull` command to run -- disk is the scarce resource here, and
a CLI that quietly pulls 6 GB because someone ran a describe is hostile.

Both fit the 12 GB card alongside a text model, and both run through the Ollama
daemon that WaterFree already depends on, so there is no second runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.llm import ollama_client

SMALL = "small"
LARGE = "large"
TIERS: tuple[str, ...] = (SMALL, LARGE)


@dataclass(frozen=True)
class VisionModel:
    tier: str
    model_id: str          # Ollama tag
    disk_gb: float
    notes: str


# Env overrides let a user swap in any Ollama vision model (llava, llama3.2-vision,
# a larger qwen2.5vl) without touching code -- the tier is the contract, not the id.
_DEFAULT_SMALL = os.environ.get("WATERFREE_VISION_MODEL_SMALL", "moondream").strip()
_DEFAULT_LARGE = os.environ.get("WATERFREE_VISION_MODEL_LARGE", "qwen2.5vl:7b").strip()

REGISTRY: dict[str, VisionModel] = {
    SMALL: VisionModel(
        tier=SMALL,
        model_id=_DEFAULT_SMALL,
        disk_gb=1.7,
        notes="Open-ended description and bulk passes. Returns '' for yes/no questions.",
    ),
    LARGE: VisionModel(
        tier=LARGE,
        model_id=_DEFAULT_LARGE,
        disk_gb=6.0,
        notes="Triage, UI critique, reading on-screen text, inferring what a page does.",
    ),
}


# ---------------------------------------------------------------------------
# Purposes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Purpose:
    key: str
    tier: str
    summary: str
    system: str
    default_question: str


_UI_SYSTEM = (
    "You are a senior front-end engineer reviewing a rendered interface from a "
    "screenshot. Describe only what is visibly present. Never guess at code, "
    "framework, or behaviour you cannot see. If the image is cut off or "
    "unreadable, say so instead of filling the gap."
)

PURPOSES: dict[str, Purpose] = {
    "describe": Purpose(
        key="describe",
        tier=SMALL,
        summary="Short factual description of what the image shows.",
        system=(
            "You describe images factually and briefly. State only what is "
            "visible. No interpretation, no speculation."
        ),
        default_question="Describe what this image shows in two or three sentences.",
    ),
    "triage": Purpose(
        key="triage",
        # Large tier despite being the cheapest-sounding job. The small tier
        # returns an empty string for yes/no and verdict-token questions (see
        # the module docstring), and a triage that silently answers "" is worse
        # than no triage at all.
        tier=LARGE,
        summary="Fast pass/fail check that a rendered page looks intact.",
        system=(
            "You check rendered screenshots for obvious breakage. Answer "
            "briefly and concretely."
        ),
        default_question=(
            "Does this page look correctly rendered? Call out blank areas, "
            "overlapping or clipped elements, missing images, unstyled text, "
            "and visible error messages. Start your answer with OK or BROKEN."
        ),
    ),
    "ui": Purpose(
        key="ui",
        tier=LARGE,
        summary="Layout and usability critique of a rendered screen.",
        system=_UI_SYSTEM,
        default_question=(
            "Review this interface. Cover: visual hierarchy, alignment and "
            "spacing, colour and contrast (flag likely accessibility problems), "
            "and anything that looks broken or unfinished. Give concrete, "
            "actionable notes ordered by impact, not general design advice."
        ),
    ),
    "function": Purpose(
        key="function",
        tier=LARGE,
        summary="Infer what the page or app is for and what a user can do on it.",
        system=_UI_SYSTEM,
        default_question=(
            "What is this page for, and what can a user actually do on it? "
            "List the primary purpose, the main interactive affordances you can "
            "see (buttons, forms, navigation), and the apparent primary action. "
            "Mark anything you are inferring rather than reading as UNCERTAIN."
        ),
    ),
    "text": Purpose(
        key="text",
        tier=LARGE,
        summary="Transcribe the visible on-screen text.",
        system=(
            "You transcribe text from screenshots verbatim. Preserve reading "
            "order and grouping. Never correct spelling or invent text that is "
            "not legible -- write [illegible] instead."
        ),
        default_question=(
            "Transcribe all visible text, preserving reading order and grouping "
            "related labels together."
        ),
    ),
    "compare": Purpose(
        key="compare",
        tier=LARGE,
        summary="Differences between two or more images.",
        system=_UI_SYSTEM + " Inspect each image independently before comparing. Describe visible overlap with its location. If detail is too small to establish overlap, say UNCERTAIN; do not conclude that no overlap exists. Your assessment is advisory and requires direct inspection for acceptance.",
        default_question=(
            "These images are versions of the same screen. List the concrete "
            "visual differences between them in order of significance, and say "
            "which version looks more finished and why."
        ),
    ),
    "classify": Purpose(
        key="classify",
        # Picking a label is a verdict, which the small tier answers with "".
        tier=LARGE,
        summary=(
            "Pick one of --labels a,b,c for any image (render, sprite, photo or "
            "screenshot); JSON {label, evidence, confidence}. Requires --labels."
        ),
        # Deliberately not _UI_SYSTEM: the identity checks this exists for are
        # of renders and sprites, and a "you are reviewing a web page" framing
        # pulls the answer toward UI vocabulary.
        system=(
            "You classify an image. It may be a 3D render, a game sprite, a photo "
            "or a screenshot. Base your choice only on what is visible. The labels "
            "are offered in no particular order, and no label is presumed right. "
            "Choose 'none' when no label fits and 'uncertain' when the image does "
            "not let you decide. Reply with JSON only."
        ),
        default_question="Which one label best describes what this image shows?",
    ),
}

DEFAULT_PURPOSE = "describe"


class VisionModelMissing(RuntimeError):
    """A required vision model is not downloaded. Message names the fix."""


def get_purpose(key: str) -> Purpose:
    normalized = str(key or DEFAULT_PURPOSE).strip().lower()
    purpose = PURPOSES.get(normalized)
    if purpose is None:
        known = ", ".join(sorted(PURPOSES))
        raise ValueError(f"Unknown purpose '{key}'. Choose one of: {known}")
    return purpose


@dataclass(frozen=True)
class Route:
    model_id: str
    tier: str          # registry tier, or "" for an ad-hoc --model
    reason: str        # surfaced as `tierReason`, so a caller can see why


def resolve_route(
    purpose: Purpose, *, override: str = "", tier: str = "", question: str = "",
) -> Route:
    """Which model answers, and why.

    Precedence: an explicit model, then an explicit tier, then the purpose's
    tier -- except that a caller's own question (`-q`) is lifted to the large
    tier. The small tier is fine for the stock describe prompt, but it echoes
    whatever candidates a question names: asked whether a trestle table or a
    goblet was visible, moondream said yes to both for images with neither,
    while the large tier answered a neutral question accurately. Custom
    questions are where agents ask about specifics, so they get the model that
    does not agree with them. An explicit --tier small still wins.
    """
    if override.strip():
        model_id = override.strip()
        return Route(model_id, tier_of(model_id), "--model given")
    if tier.strip():
        chosen = tier.strip().lower()
        if chosen not in REGISTRY:
            raise ValueError(f"Unknown tier '{tier}'. Choose one of: {', '.join(TIERS)}")
        return Route(REGISTRY[chosen].model_id, chosen, f"--tier {chosen} given")
    if question.strip() and purpose.tier != LARGE:
        return Route(
            REGISTRY[LARGE].model_id,
            LARGE,
            f"custom question: large tier instead of {purpose.key}'s {purpose.tier} "
            f"tier, which tends to agree with what a question names (--tier "
            f"{purpose.tier} to override)",
        )
    return Route(REGISTRY[purpose.tier].model_id, purpose.tier, f"default for purpose '{purpose.key}'")


def resolve_model(
    purpose: Purpose, *, override: str = "", tier: str = "", question: str = "",
) -> str:
    """Model id for a purpose. See `resolve_route` for the precedence."""
    return resolve_route(purpose, override=override, tier=tier, question=question).model_id


def tier_of(model_id: str) -> str:
    """The tier whose registry entry is `model_id`, or "" for an ad-hoc model."""
    return next((t for t, m in REGISTRY.items() if m.model_id == model_id), "")


def missing_model_error(model_id: str) -> VisionModelMissing:
    """The actionable "not downloaded" error, naming the exact pull command."""
    tier = tier_of(model_id)
    size = f" (~{REGISTRY[tier].disk_gb} GB)" if tier else ""
    hint = f"waterfree vision pull --tier {tier}" if tier else f"ollama pull {model_id}"
    return VisionModelMissing(
        f"Vision model '{model_id}'{size} is not downloaded. Get it with:\n  {hint}"
    )


# How long the pre-flight "is the model downloaded?" check waits on /api/tags.
# A timeout here is not fatal (see ensure_available), so this only bounds how
# long a busy host can delay the real request -- it is not a correctness knob.
LIST_TIMEOUT_SECONDS = float(os.environ.get("WATERFREE_VISION_LIST_TIMEOUT", "10"))

INSTALLED = "installed"
UNVERIFIED = "unverified"


def ensure_available(model_id: str, *, base: str = "", timeout: float | None = None) -> str:
    """Raise with the pull command when `model_id` is not installed.

    Returns INSTALLED, or UNVERIFIED when Ollama was too busy to list its
    models in time. A slow /api/tags is the signature of a loaded host, not of
    a missing model, and the chat call that follows reports a missing model by
    itself (HTTP 404) -- so failing the whole look here would turn "the host is
    busy" into "nothing works". A daemon that refuses the connection still
    raises: generation would fail the same way, only later.
    """
    kwargs = {"base": base} if base else {}
    list_timeout = LIST_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        present = ollama_client.has_model(model_id, strict=True, timeout=list_timeout, **kwargs)
    except ollama_client.OllamaTimeout:
        return UNVERIFIED
    if present:
        return INSTALLED
    raise missing_model_error(model_id)


def installed_report(base: str = "", timeout: float | None = None) -> list[dict]:
    """Tier table with download state, for `waterfree vision models`.

    `installed` is None when Ollama could not be asked (down or too busy):
    reporting False there would send the caller off to pull gigabytes it
    already has.
    """
    kwargs = {"base": base} if base else {}
    try:
        names: list[str] | None = ollama_client.list_models(
            timeout=LIST_TIMEOUT_SECONDS if timeout is None else timeout, **kwargs,
        )
    except ollama_client.OllamaError:
        names = None
    report = []
    for tier in TIERS:
        entry = REGISTRY[tier]
        report.append({
            "tier": tier,
            "model": entry.model_id,
            "diskGb": entry.disk_gb,
            "installed": None if names is None else ollama_client.model_in(entry.model_id, names),
            "purposes": sorted(p.key for p in PURPOSES.values() if p.tier == tier),
            "notes": entry.notes,
        })
    return report
