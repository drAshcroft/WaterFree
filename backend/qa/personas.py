"""Personas: who is sitting at the keyboard.

A persona is a markdown file with front matter (viewport, pacing, tags) and a
body that becomes a fragment of the system prompt. Built-ins live beside this
module; a workspace can add or override with `.waterfree/qa/personas/*.md`.

The persona is where the "normal person mistakes" come from. The model runs
cold and literal; the persona tells it to be impatient, or to read nothing, or
to try to cheat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

BUILTIN_DIR = Path(__file__).parent / "personas"
WORKSPACE_REL = Path(".waterfree") / "qa" / "personas"
DEFAULT_PERSONA = "first-timer"

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


class PersonaNotFound(KeyError):
    pass


@dataclass(frozen=True)
class Persona:
    name: str
    summary: str
    body: str
    viewport: tuple[int, int] = (1280, 800)
    mobile: bool = False
    pacing: str = "normal"          # normal | fast | slow  -- how long WAIT/settle takes
    forbid: tuple[str, ...] = ()    # verbs this persona may not use (keyboard-only forbids CLICK)
    tags: tuple[str, ...] = ()
    source: str = "builtin"
    path: str = field(default="", compare=False)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "summary": self.summary,
            "viewport": f"{self.viewport[0]}x{self.viewport[1]}",
            "mobile": self.mobile,
            "pacing": self.pacing,
            "forbid": list(self.forbid),
            "tags": list(self.tags),
            "source": self.source,
        }


def _parse_front(text: str) -> tuple[dict, str]:
    m = _FRONT.match(text.lstrip("﻿"))
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip()
    return meta, m.group(2).strip()


def _parse_viewport(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", raw or "")
    if not m:
        return default
    return int(m.group(1)), int(m.group(2))


def _split_list(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in re.split(r"[,\s]+", raw or "") if p.strip())


def load_persona_file(path: Path, *, source: str) -> Persona:
    meta, body = _parse_front(path.read_text(encoding="utf-8"))
    name = (meta.get("name") or path.stem).strip().lower()
    return Persona(
        name=name,
        summary=meta.get("summary", "").strip(),
        body=body,
        viewport=_parse_viewport(meta.get("viewport", ""), (1280, 800)),
        mobile=meta.get("mobile", "").strip().lower() in {"1", "true", "yes"},
        pacing=(meta.get("pacing") or "normal").strip().lower(),
        forbid=tuple(v.upper() for v in _split_list(meta.get("forbid", ""))),
        tags=_split_list(meta.get("tags", "")),
        source=source,
        path=str(path),
    )


def registry(workspace: str | Path | None = None) -> dict[str, Persona]:
    """Built-ins, overridden by any same-named file in the workspace."""
    found: dict[str, Persona] = {}
    if BUILTIN_DIR.is_dir():
        for path in sorted(BUILTIN_DIR.glob("*.md")):
            p = load_persona_file(path, source="builtin")
            found[p.name] = p
    if workspace:
        ws_dir = Path(workspace) / WORKSPACE_REL
        if ws_dir.is_dir():
            for path in sorted(ws_dir.glob("*.md")):
                p = load_persona_file(path, source="workspace")
                found[p.name] = p
    return found


def resolve(names: list[str] | tuple[str, ...], workspace: str | Path | None = None) -> list[Persona]:
    reg = registry(workspace)
    if not names:
        names = [DEFAULT_PERSONA]
    out: list[Persona] = []
    for raw in names:
        key = raw.strip().lower()
        if key not in reg:
            raise PersonaNotFound(
                f"Unknown persona '{raw}'. Available: {', '.join(sorted(reg))}"
            )
        out.append(reg[key])
    return out
