"""
Context formatting utilities — text excerpting, architecture formatting, file reads.

Pure functions with no external dependencies beyond stdlib.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from backend.knowledge import retriever as knowledge_retriever


def format_architecture(arch: dict) -> str:
    parts: list[str] = []

    langs = arch.get("languages")
    if langs:
        lang_str = ", ".join(
            f"{l['name']} ({l.get('file_count', '?')} files)"
            for l in (langs if isinstance(langs, list) else [])
        )
        parts.append(f"Languages: {lang_str}")

    entry_points = arch.get("entry_points") or []
    if entry_points:
        ep_names = [ep.get("name") or ep.get("qualified_name", "?") for ep in entry_points[:5]]
        parts.append(f"Entry points: {', '.join(ep_names)}")

    layers = arch.get("layers") or []
    if layers:
        layer_names = [la.get("name", str(la)) for la in layers[:6]]
        parts.append(f"Layers: [{', '.join(layer_names)}]")

    hotspots = arch.get("hotspots") or []
    if hotspots:
        hs_lines = [
            f"  {h.get('name', '?')} ({h.get('in_degree', '?')} callers)"
            for h in hotspots[:8]
        ]
        parts.append("Hotspots (most-called):\n" + "\n".join(hs_lines))

    clusters = arch.get("clusters") or []
    if clusters:
        cl_lines = []
        for c in clusters[:6]:
            members = c.get("members") or []
            sample = ", ".join(m.get("name", str(m)) for m in members[:4])
            ellipsis = "…" if len(members) > 4 else ""
            cl_lines.append(f"  Cluster {c.get('id', '?')}: {sample}{ellipsis}")
        parts.append("Functional clusters (Louvain):\n" + "\n".join(cl_lines))

    return "\n".join(parts) if parts else "(no architecture data)"


def format_adr(adr: Optional[dict]) -> str:
    if not adr:
        return ""
    text = adr.get("text", "")
    if not text:
        sections = adr.get("sections", {})
        text = "\n\n".join(
            f"## {k}\n{v}" for k, v in sections.items() if v
        )
    if not text:
        return ""
    return f"ARCHITECTURE DECISION RECORD:\n{text}\n\n"


def build_design_inputs(
    *,
    workspace_path: str,
    session_goal: str,
    query: str,
    current_task=None,
) -> str:
    keywords = extract_keywords(query)
    sections = ["DESIGN INPUTS:"]
    sections.append(f"SESSION GOAL:\n{session_goal}")

    if current_task:
        sections.append(
            "CURRENT TASK:\n"
            f"{current_task.title}\n"
            f"{current_task.description}"
        )

    plan_text = read_workspace_note(workspace_path, ".waterfree", "plan.md")
    if plan_text:
        sections.append(
            ".waterfree/plan.md:\n"
            f"{excerpt_text(plan_text, keywords, max_chars=900)}"
        )

    memory_text = read_pairs_memory(workspace_path)
    if memory_text:
        sections.append(
            ".waterfree/memory.md:\n"
            f"{excerpt_text(memory_text, keywords, max_chars=900)}"
        )

    matched_docs = select_design_docs(workspace_path, keywords, limit=2)
    if matched_docs:
        doc_sections = []
        for rel_path, excerpt in matched_docs:
            doc_sections.append(f"{rel_path}:\n{excerpt}")
        sections.append("MATCHED DOCS:\n" + "\n\n".join(doc_sections))

    return "\n\n".join(sections) + "\n\n"


def search_knowledge(query: str) -> str:
    """Return a formatted knowledge section, or empty string if nothing found."""
    section = knowledge_retriever.search_for_context(query)
    return f"\n\n{section}" if section else ""


def read_file(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_workspace_note(workspace_path: str, *parts: str) -> Optional[str]:
    target = Path(workspace_path).joinpath(*parts)
    try:
        text = target.read_text(encoding="utf-8", errors="replace").strip()
        return text if text else None
    except OSError:
        return None


def read_pairs_memory(workspace_path: str) -> Optional[str]:
    """Read .waterfree/memory.md if it exists."""
    memory_path = Path(workspace_path) / ".waterfree" / "memory.md"
    try:
        text = memory_path.read_text(encoding="utf-8", errors="replace").strip()
        return text if text else None
    except OSError:
        return None


def extract_keywords(text: str) -> list[str]:
    stop_words = {
        "the", "and", "for", "with", "this", "that", "from", "into", "then", "when",
        "have", "has", "will", "your", "task", "goal", "plan", "rough", "roughing",
        "stub", "stubs", "wireframe", "wireframes", "subsystem", "feature",
    }
    seen: set[str] = set()
    keywords: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_]+", text.casefold()):
        if len(token) < 2 or token in stop_words or token in seen:
            continue
        seen.add(token)
        keywords.append(token)
    return keywords[:16]


def excerpt_text(text: str, keywords: list[str], max_chars: int = 900) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    selected: list[str] = []
    if keywords:
        for line in lines:
            lower_line = line.casefold()
            if any(keyword in lower_line for keyword in keywords):
                selected.append(line)
            if sum(len(item) + 1 for item in selected) >= max_chars:
                break

    if not selected:
        selected = lines

    excerpt_lines: list[str] = []
    total = 0
    for line in selected:
        addition = len(line) + 1
        if total + addition > max_chars:
            break
        excerpt_lines.append(line)
        total += addition

    if not excerpt_lines:
        excerpt_lines.append(selected[0][:max_chars])

    excerpt = "\n".join(excerpt_lines)
    if len(excerpt) < len("\n".join(selected)):
        excerpt += "\n..."
    return excerpt


def select_design_docs(
    workspace_path: str,
    keywords: list[str],
    limit: int = 2,
) -> list[tuple[str, str]]:
    docs_dir = Path(workspace_path) / "docs"
    if not docs_dir.exists():
        return []

    matches: list[tuple[int, str, str]] = []
    for path in sorted(docs_dir.glob("*.md")):
        text = read_file(str(path))
        if not text:
            continue
        search_space = f"{path.name}\n{text[:6000]}".casefold()
        score = sum(search_space.count(keyword) for keyword in keywords) if keywords else 0
        if score <= 0:
            continue
        rel_path = path.relative_to(Path(workspace_path)).as_posix()
        excerpt = excerpt_text(text, keywords, max_chars=900)
        matches.append((score, rel_path, excerpt))

    matches.sort(key=lambda item: (-item[0], item[1]))
    return [(rel_path, excerpt) for _, rel_path, excerpt in matches[:limit]]
