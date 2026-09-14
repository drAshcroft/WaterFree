"""Prompt builders for the driver loop. Pure functions, unit-tested without Ollama.

The whole turn should stay under about 2,500 tokens: a system prompt that does
not change during a run (so Ollama's prefix cache helps), and a user turn with
the goal, recent history and the current snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.qa.actions import GRAMMAR_HELP
from backend.qa.personas import Persona
from backend.qa.snapshot import Snapshot, similar

HISTORY_TURNS = 6


@dataclass
class HistoryItem:
    step: int
    action: str
    outcome: str


@dataclass
class RunBrief:
    goal: str
    hints: list[str] = field(default_factory=list)
    qa_md: str = ""           # verbatim QA.md from the target repo, if any
    playbook: str = ""        # genre playbook text, if any


_HARNESS_RULES = """You are testing a web app by using it, one action at a time, like a real person.
You see a numbered list of the controls on screen and the visible text. You reply with ONE action line.

Rules:
- Reply with exactly one action line and nothing else. No explanation, no markdown.
- Only use element numbers that appear in the current list.
- Elements marked disabled or offscreen cannot be clicked; SCROLL first or pick another.
- When a dialog is open, deal with it before trying anything behind it.
- Do not repeat an action that already had no effect. Try something different or NOTE the problem.
- WAIT only when the page says it is loading or you just did something that needs a moment.
- Record problems as you meet them with NOTE. Severity: high blocks the goal or loses data;
  medium is wrong behaviour or an instruction that would lose a new player; low is cosmetic
  or a wording nit; note is praise, a question, or an observation.
- Every NOTE must say what you did, what you saw, and what you expected, in the detail part.
- When the goal is reached, or you have tried everything reasonable, reply DONE with a summary.
- If you truly cannot make progress after several different attempts, reply STUCK and say why.
- The app may be a game. Play it as the player described below would."""


def system_prompt(persona: Persona, brief: RunBrief) -> str:
    parts = [_HARNESS_RULES, "", "Actions:", GRAMMAR_HELP]
    if persona.forbid:
        parts += ["", "For this run you may NOT use: " + ", ".join(persona.forbid) + "."]
    parts += ["", f"Who you are ({persona.name}):", persona.body.strip()]
    if brief.playbook.strip():
        parts += ["", "Things worth trying in this kind of game:", brief.playbook.strip()]
    if brief.qa_md.strip():
        parts += ["", "Notes from the developers about this app:", brief.qa_md.strip()]
    return "\n".join(parts)


def turn_prompt(
    brief: RunBrief,
    snapshot: Snapshot,
    history: list[HistoryItem],
    *,
    previous: Snapshot | None = None,
    step: int = 1,
    steps_left: int | None = None,
    error: str = "",
) -> str:
    parts = [f"GOAL: {brief.goal.strip()}"]
    if brief.hints:
        parts.append("HINTS:")
        parts += [f"- {h.strip()}" for h in brief.hints if h.strip()]
    if steps_left is not None:
        parts.append(f"Step {step}. {steps_left} steps left before the run ends.")
    else:
        parts.append(f"Step {step}.")
    if history:
        parts.append("")
        parts.append("What you did recently:")
        for item in history[-HISTORY_TURNS:]:
            parts.append(f"  {item.step}. {item.action} -> {item.outcome}")
    if error:
        parts.append("")
        parts.append("PROBLEM WITH YOUR LAST REPLY: " + error.strip())
    parts.append("")
    parts.append("SCREEN NOW:")
    if previous is not None and similar(snapshot, previous):
        parts.append(snapshot.render_diff(previous))
    else:
        parts.append(snapshot.render())
    parts.append("")
    parts.append("Your next action (one line):")
    return "\n".join(parts)


def estimate_tokens(text: str) -> int:
    """Rough token estimate for budget reporting. Four characters per token."""
    return max(1, len(text) // 4)
