"""Where the model's local knowledge comes from.

* `QA.md` in the target workspace: the developers' map of the app, read
  verbatim. Kept short on purpose; past about 150 lines it stops being a hint
  and starts crowding out the screen.
* Playbooks: knowledge-base entries tagged `qa-playbook` plus a genre tag.
  `--hint genre:card-game` pulls them in.
"""

from __future__ import annotations

import sys
from pathlib import Path

QA_MD_NAMES = ("QA.md", "qa.md", "docs/QA.md")
QA_MD_MAX_LINES = 150
GENRE_PREFIX = "genre:"
PLAYBOOK_TAG = "qa-playbook"


def load_qa_md(workspace: str | Path | None) -> str:
    if not workspace:
        return ""
    root = Path(workspace)
    for name in QA_MD_NAMES:
        path = root / name
        if path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > QA_MD_MAX_LINES:
                sys.stderr.write(
                    f"qa: {path} is {len(lines)} lines; only the first {QA_MD_MAX_LINES} are used.\n"
                )
                lines = lines[:QA_MD_MAX_LINES]
            return "\n".join(lines).strip()
    return ""


def split_hints(raw_hints: list[str]) -> tuple[list[str], list[str]]:
    """Separate `genre:x` hints (playbook lookups) from plain hints."""
    genres: list[str] = []
    plain: list[str] = []
    for h in raw_hints:
        h = h.strip()
        if not h:
            continue
        if h.lower().startswith(GENRE_PREFIX):
            genres.append(h[len(GENRE_PREFIX):].strip().lower())
        else:
            plain.append(h)
    return plain, genres


def load_playbooks(genres: list[str]) -> str:
    """Concatenate knowledge-base playbooks for the requested genres.

    Imported lazily: the knowledge store opens SQLite, and `qa personas` should
    not pay for that.
    """
    if not genres:
        return ""
    try:
        from backend.knowledge.store import KnowledgeStore  # noqa: PLC0415
    except Exception:  # pragma: no cover - store unavailable in a stripped build
        return ""
    store = KnowledgeStore()
    try:
        chunks: list[str] = []
        seen: set[str] = set()
        for genre in genres:
            for entry in store.search(f"{PLAYBOOK_TAG} {genre}", limit=5):
                tags = {t.lower() for t in (entry.tags or [])}
                if PLAYBOOK_TAG not in tags or genre not in tags or entry.id in seen:
                    continue
                seen.add(entry.id)
                chunks.append(f"## {entry.title}\n{entry.code.strip()}")
        if not chunks:
            sys.stderr.write(
                f"qa: no playbook found for genre(s) {', '.join(genres)}; "
                f"add knowledge entries tagged '{PLAYBOOK_TAG}' and the genre.\n"
            )
        return "\n\n".join(chunks)
    finally:
        store.close()
