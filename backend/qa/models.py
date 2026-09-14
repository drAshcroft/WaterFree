"""Which local text model drives the QA loop.

Two tiers, like vision: a default that fits beside the vision model on a 12 GB
card, and a slower fallback for when the default keeps emitting unparseable
actions. Both are already-pulled Ollama tags on the reference machine.
Nothing is downloaded implicitly; a missing model names the pull command.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.llm import ollama_client

DEFAULT = "default"
FALLBACK = "fallback"
TIERS: tuple[str, ...] = (DEFAULT, FALLBACK)


@dataclass(frozen=True)
class DriverModel:
    tier: str
    model_id: str
    notes: str


REGISTRY: dict[str, DriverModel] = {
    DEFAULT: DriverModel(
        tier=DEFAULT,
        model_id=os.environ.get("WATERFREE_QA_MODEL", "qwen3.5:9b").strip(),
        notes="Fast enough for a step every few seconds; follows the one-line grammar well.",
    ),
    FALLBACK: DriverModel(
        tier=FALLBACK,
        model_id=os.environ.get("WATERFREE_QA_MODEL_FALLBACK", "qwen2.5:14b").strip(),
        notes="Slower, steadier. Used when the default repeatedly fails to produce an action.",
    ),
}

KEEP_ALIVE = os.environ.get("WATERFREE_QA_KEEP_ALIVE", "20m")
TIMEOUT_SECONDS = int(os.environ.get("WATERFREE_QA_STEP_TIMEOUT", "120"))
# Low temperature: we want the most likely action, not creativity. Persona
# prompts supply the "human" variance instead.
OPTIONS: dict = {"temperature": 0.2, "num_predict": 160}


class DriverModelMissing(RuntimeError):
    """The model is not pulled. The message contains the exact fix."""


def resolve_model(override: str = "", tier: str = "") -> str:
    if override.strip():
        return override.strip()
    return REGISTRY[tier or DEFAULT].model_id


def ensure_available(model_id: str, *, base: str = "") -> None:
    kwargs = {"base": base} if base else {}
    try:
        present = ollama_client.has_model(model_id, **kwargs)
    except ollama_client.OllamaError as exc:
        raise DriverModelMissing(str(exc)) from exc
    if not present:
        raise DriverModelMissing(
            f"Driver model '{model_id}' is not downloaded. Run: ollama pull {model_id}"
        )


def installed_report() -> list[dict]:
    try:
        installed = set(ollama_client.list_models())
    except ollama_client.OllamaError:
        installed = set()

    def present(model_id: str) -> bool:
        return model_id in installed or model_id.split(":")[0] in installed

    return [
        {"tier": m.tier, "model": m.model_id, "downloaded": present(m.model_id), "notes": m.notes}
        for m in REGISTRY.values()
    ]
