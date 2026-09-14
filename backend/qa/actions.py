"""The action grammar: the contract between the driver model and Playwright.

One line, one verb. Small local models are reliable at emitting a fixed
one-line format and unreliable at JSON tool calls under Ollama, so the grammar
is deliberately plain text:

    CLICK <n>                      click numbered element n
    TYPE <n> "text"                clear element n and type text into it
    PRESS <key>                    keyboard key: Enter, Escape, Tab, ArrowLeft, Space, a, 1
    SCROLL up|down                 scroll the page by most of a viewport
    WAIT <seconds>                 wait, at most 5 seconds
    BACK                           browser back
    LOOK "question"                ask the vision model about the screen
    NOTE <sev> "title" :: "detail" record a finding (high | medium | low | note)
    DONE "summary"                 goal reached, or nothing more to try
    STUCK "why"                    cannot make progress

The parser tolerates the noise a small model adds -- prose before the action,
code fences, a trailing explanation -- and takes the first line that parses.
Anything else becomes a ParseError whose message is written for the model, so
the driver can hand it straight back as the next user turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VERBS: tuple[str, ...] = (
    "CLICK", "TYPE", "PRESS", "SCROLL", "WAIT", "BACK", "LOOK", "NOTE", "DONE", "STUCK",
)
SEVERITIES: tuple[str, ...] = ("high", "medium", "low", "note")
MAX_WAIT_SECONDS = 5

# Verbs that read nothing from the page and so never need a snapshot to be valid.
TERMINAL_VERBS: frozenset[str] = frozenset({"DONE", "STUCK"})


class ParseError(ValueError):
    """The model's reply contained no valid action. The message is model-facing."""


@dataclass(frozen=True)
class Action:
    verb: str
    target: int | None = None          # element number for CLICK / TYPE
    text: str = ""                      # TYPE text, PRESS key, SCROLL direction, LOOK/DONE/STUCK text, NOTE title
    detail: str = ""                    # NOTE detail
    severity: str = ""                  # NOTE severity
    seconds: int = 0                    # WAIT
    raw: str = field(default="", compare=False)

    def describe(self) -> str:
        """One line for logs and for the model's own history."""
        if self.verb in ("CLICK",):
            return f"CLICK {self.target}"
        if self.verb == "TYPE":
            return f'TYPE {self.target} "{self.text}"'
        if self.verb == "PRESS":
            return f"PRESS {self.text}"
        if self.verb == "SCROLL":
            return f"SCROLL {self.text}"
        if self.verb == "WAIT":
            return f"WAIT {self.seconds}"
        if self.verb == "BACK":
            return "BACK"
        if self.verb == "NOTE":
            return f'NOTE {self.severity} "{self.text}" :: "{self.detail}"'
        return f'{self.verb} "{self.text}"'

    @property
    def is_terminal(self) -> bool:
        return self.verb in TERMINAL_VERBS


GRAMMAR_HELP = """Reply with exactly one action on one line. Nothing else.
  CLICK <n>                        click element number n
  TYPE <n> "text"                  type text into element n
  PRESS <key>                      Enter, Escape, Tab, Space, ArrowUp/Down/Left/Right, or a single character
  SCROLL up | SCROLL down          scroll the page
  WAIT <seconds>                   wait up to 5 seconds for the page to change
  BACK                             browser back button
  LOOK "question"                  ask what the screen actually looks like
  NOTE <high|medium|low|note> "short title" :: "what you did, what you saw, what you expected"
  DONE "one sentence summary"      the goal is reached or there is nothing left to try
  STUCK "why"                      you cannot make progress"""


_QUOTED = r'"((?:[^"\\]|\\.)*)"'
_RE_CLICK = re.compile(r"^CLICK\s+#?(\d+)\s*$", re.I)
_RE_TYPE = re.compile(rf"^TYPE\s+#?(\d+)\s+{_QUOTED}\s*$", re.I)
_RE_TYPE_BARE = re.compile(r"^TYPE\s+#?(\d+)\s+(\S.*?)\s*$", re.I)
_RE_PRESS = re.compile(r"^PRESS\s+\"?([A-Za-z0-9+]+)\"?\s*$", re.I)
_RE_SCROLL = re.compile(r"^SCROLL\s+(up|down)\s*$", re.I)
_RE_WAIT = re.compile(r"^WAIT\s*(\d+(?:\.\d+)?)?\s*(?:s|sec|seconds)?\s*$", re.I)
_RE_BACK = re.compile(r"^BACK\s*$", re.I)
_RE_SIMPLE_TEXT = re.compile(rf"^(LOOK|DONE|STUCK)\s*(?::)?\s*(?:{_QUOTED}|(\S.*?))?\s*$", re.I)
_RE_NOTE = re.compile(
    rf"^NOTE\s+(high|medium|low|note)\s*:?\s*{_QUOTED}\s*(?:::|-|—|:)?\s*(?:{_QUOTED}|(.*?))?\s*$",
    re.I,
)
_RE_NOTE_BARE = re.compile(
    r"^NOTE\s+(high|medium|low|note)\s*:?\s*(.+?)\s*(?:::|—)\s*(.+?)\s*$", re.I
)

_LEADING_NOISE = re.compile(r"^(?:[-*>•]+|\d+[.)])\s*|^(?:action|answer|next)\s*:\s*", re.I)


def _unescape(text: str) -> str:
    return text.replace('\\"', '"').replace("\\n", " ").strip()


def _parse_line(line: str) -> Action | None:
    line = line.strip().strip("`").strip()
    line = _LEADING_NOISE.sub("", line).strip()
    if not line:
        return None
    head = line.split(None, 1)[0].upper().rstrip(":")
    if head not in VERBS:
        return None

    m = _RE_CLICK.match(line)
    if m:
        return Action("CLICK", target=int(m.group(1)), raw=line)

    m = _RE_TYPE.match(line)
    if m:
        return Action("TYPE", target=int(m.group(1)), text=_unescape(m.group(2)), raw=line)
    m = _RE_TYPE_BARE.match(line)
    if m:
        return Action("TYPE", target=int(m.group(1)), text=m.group(2).strip().strip('"'), raw=line)

    m = _RE_PRESS.match(line)
    if m:
        return Action("PRESS", text=_normalize_key(m.group(1)), raw=line)

    m = _RE_SCROLL.match(line)
    if m:
        return Action("SCROLL", text=m.group(1).lower(), raw=line)

    m = _RE_WAIT.match(line)
    if m:
        seconds = int(float(m.group(1))) if m.group(1) else 1
        return Action("WAIT", seconds=max(1, min(MAX_WAIT_SECONDS, seconds)), raw=line)

    if _RE_BACK.match(line):
        return Action("BACK", raw=line)

    m = _RE_NOTE.match(line)
    if m:
        detail = m.group(3) if m.group(3) is not None else (m.group(4) or "")
        return Action(
            "NOTE",
            severity=m.group(1).lower(),
            text=_unescape(m.group(2)),
            detail=_unescape(detail).strip('"'),
            raw=line,
        )
    m = _RE_NOTE_BARE.match(line)
    if m:
        return Action(
            "NOTE",
            severity=m.group(1).lower(),
            text=m.group(2).strip().strip('"'),
            detail=m.group(3).strip().strip('"'),
            raw=line,
        )

    m = _RE_SIMPLE_TEXT.match(line)
    if m:
        text = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        return Action(m.group(1).upper(), text=_unescape(text).strip('"'), raw=line)

    return None


_KEY_ALIASES = {
    "return": "Enter", "enter": "Enter", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": "Space", "spacebar": "Space", "backspace": "Backspace",
    "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
    "right": "ArrowRight", "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft", "arrowright": "ArrowRight", "home": "Home", "end": "End",
    "pageup": "PageUp", "pagedown": "PageDown",
}


def _normalize_key(key: str) -> str:
    lowered = key.lower()
    if lowered in _KEY_ALIASES:
        return _KEY_ALIASES[lowered]
    if "+" in key:  # chords such as Shift+Tab
        return "+".join(_normalize_key(part) for part in key.split("+"))
    return key


def parse(reply: str) -> Action:
    """Return the first valid action in the model's reply.

    Raises ParseError with a message the model can act on.
    """
    if not reply or not reply.strip():
        raise ParseError("Your reply was empty. " + GRAMMAR_HELP)

    for line in reply.splitlines():
        action = _parse_line(line)
        if action is not None:
            return action

    # A verb appeared but did not parse; say so specifically.
    for line in reply.splitlines():
        head = line.strip().strip("`").split(None, 1)
        if head and head[0].upper().rstrip(":") in VERBS:
            raise ParseError(
                f'"{line.strip()[:80]}" is not a valid action. Check the argument shape. '
                + GRAMMAR_HELP
            )

    raise ParseError(
        "No action found in your reply. Do not explain; reply with one action line. "
        + GRAMMAR_HELP
    )
