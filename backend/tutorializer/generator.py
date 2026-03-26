"""
Tutorial generation pipeline.

Phases:
  1. Repo analysis   — Ollama reads the file tree + README and identifies key areas.
  2. Focus filtering — Ollama ranks areas by the user's stated interests.
  3. Tutorial gen    — For each area, Ollama reads relevant source files and writes
                       a Markdown tutorial.
  4. Storage         — Each tutorial is saved as a KnowledgeEntry (snippet_type="tutorial")
                       under tutorial/{repo}/{focus?}/{area} in the hierarchy.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional

from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.store import KnowledgeStore
from backend.tutorializer import ollama as _ollama
from backend.tutorializer import scanner

log = logging.getLogger(__name__)

_MAX_FILE_CHARS = 6000    # chars per source file fed to the LLM
_MAX_FILES_PER_AREA = 5   # source files included per tutorial


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:40] or "misc"


def _extract_json(text: str) -> dict:
    """Parse JSON from LLM output, tolerating markdown code fences."""
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group())
    return json.loads(cleaned)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class TutorialGenerator:
    """
    Orchestrates the full tutorialization pipeline for a single repository.

    Args:
        repo_path:    Absolute path to the repository being tutorialized.
        model:        Ollama model name (e.g. "llama3.2", "mistral").
        store:        Open KnowledgeStore to write tutorials into.
        progress_cb:  Optional callback(message: str) called at each step.
        ollama_base:  Base URL of the Ollama daemon.
        ollama_timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        repo_path: Path,
        model: str,
        store: KnowledgeStore,
        progress_cb: Optional[Callable[[str], None]] = None,
        ollama_base: str = "http://localhost:11434",
        ollama_timeout: int = 180,
    ):
        self.repo_path = repo_path
        self.repo_name = repo_path.name
        self.model = model
        self.store = store
        self.progress_cb = progress_cb or (lambda _: None)
        self._base = ollama_base
        self._timeout = ollama_timeout

    # ── Internal helpers ────────────────────────────────────────────────────

    def _chat(self, system: str, user: str) -> str:
        return _ollama.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            base=self._base,
            timeout=self._timeout,
        )

    # ── Phase 1: Repo analysis ───────────────────────────────────────────────

    def analyze_repo(self) -> dict:
        """
        Ask Ollama to identify the repo's purpose and key learning areas.
        Returns a dict with keys: overview, stack, key_areas, learning_path.
        """
        self.progress_cb("Scanning repo structure...")
        tree = scanner.file_tree(self.repo_path)
        readme = scanner.read_readme(self.repo_path)
        manifest = scanner.read_manifest(self.repo_path)

        system = (
            "You are a senior software developer helping document a repository for "
            "a new team member. Analyze the provided information and respond ONLY "
            "with valid JSON — no prose, no markdown fences."
        )
        user = f"""Repository: {self.repo_name}

File tree:
{tree}

README:
{readme[:2500] if readme else "(no README found)"}

Manifest / config:
{manifest[:1000] if manifest else "(none found)"}

Respond with exactly this JSON structure:
{{
  "overview": "2-3 sentence description of what this project is and does",
  "stack": ["list", "of", "core", "technologies"],
  "key_areas": [
    {{
      "name": "Short area name",
      "description": "What this area covers and why it matters",
      "relevant_paths": ["path/to/relevant/dir", "path/to/key/file.py"]
    }}
  ],
  "learning_path": ["area name in suggested learning order", "..."]
}}

Include 4-8 key_areas that represent the most important concepts a new developer must understand."""

        self.progress_cb("Asking Ollama to analyze the repository...")
        raw = self._chat(system, user)
        try:
            return _extract_json(raw)
        except Exception as exc:
            log.warning("Repo analysis JSON parse failed: %s\nRaw output: %.300s", exc, raw)
            # Graceful degradation — return minimal structure so the pipeline continues
            return {
                "overview": raw[:500],
                "stack": [],
                "key_areas": [{"name": "Overview", "description": raw[:300], "relevant_paths": []}],
                "learning_path": ["Overview"],
            }

    # ── Phase 2: Focus filtering ─────────────────────────────────────────────

    def filter_areas_by_focus(self, areas: list[dict], focus: str) -> list[dict]:
        """
        Use Ollama to select the subset of key_areas most relevant to the user's focus.
        Returns up to 5 areas; falls back to the first 5 if filtering fails.
        """
        if not focus or not areas:
            return areas[:5]

        self.progress_cb("Filtering areas by your stated interest...")
        area_names = [a["name"] for a in areas]

        system = "You are helping a developer prioritise their learning. Respond with valid JSON only."
        user = f"""A developer wants to learn about: "{focus}"

Available topic areas in '{self.repo_name}':
{json.dumps(area_names, indent=2)}

Choose the 3-5 areas most relevant to their interest and respond with:
{{"selected": ["area name", "..."]}}"""

        try:
            raw = self._chat(system, user)
            result = _extract_json(raw)
            selected = set(result.get("selected", []))
            filtered = [a for a in areas if a["name"] in selected]
            return filtered if filtered else areas[:5]
        except Exception as exc:
            log.warning("Area filtering failed: %s", exc)
            return areas[:5]

    # ── Phase 3: Tutorial generation ─────────────────────────────────────────

    def generate_tutorial_for_area(self, area: dict, focus: str) -> Optional[KnowledgeEntry]:
        """
        Generate a Markdown tutorial for one key area and package it as a KnowledgeEntry.
        Returns None only on hard failure.
        """
        area_name = area.get("name", "Unknown")
        hints = area.get("relevant_paths", [])

        # Find the most relevant source files for this area
        all_sources = scanner.collect_source_files(self.repo_path)
        matched = scanner.find_files_matching_hints(self.repo_path, hints, all_sources, _MAX_FILES_PER_AREA)

        # Build the source-file block fed to the LLM
        file_blocks: list[str] = []
        for path in matched[:_MAX_FILES_PER_AREA]:
            rel = str(path.relative_to(self.repo_path)).replace("\\", "/")
            content = scanner.read_file_safe(path, _MAX_FILE_CHARS)
            file_blocks.append(f"### {rel}\n```\n{content}\n```")
        files_text = "\n\n".join(file_blocks) if file_blocks else "(no source files located)"

        focus_clause = f" The reader is particularly interested in: {focus}." if focus else ""

        system = (
            "You are writing practical developer tutorials for someone new to a codebase. "
            "Your tutorials are clear, grounded in the actual source code, and actionable."
        )
        user = f"""Write a tutorial for the '{area_name}' area of the '{self.repo_name}' repository.{focus_clause}

Area description: {area.get('description', '')}

Source files to reference:
{files_text}

Structure the tutorial in Markdown with these sections:
## Overview
What this area does and why it exists in the project.

## Key Concepts
Core ideas the reader needs before diving into the code.

## Code Walkthrough
Step-by-step explanation of how the important code works.
Reference specific functions, classes, or patterns from the files above.

## Patterns & Conventions
Recurring patterns, naming conventions, or design decisions used in this area.

## Next Steps
Related areas to explore and prerequisites to review first.

Be specific — reference actual code from the provided files. Aim for 350-600 words."""

        self.progress_cb(f"Generating tutorial: {area_name}...")
        try:
            tutorial_md = self._chat(system, user)
        except _ollama.OllamaError:
            raise
        except Exception as exc:
            log.error("Tutorial generation failed for '%s': %s", area_name, exc)
            return None

        # Extract a short description from the tutorial for the knowledge store
        try:
            description = self._chat(
                "Summarise the following tutorial in 2-3 sentences. Output only the summary.",
                tutorial_md[:2000],
            ).strip()
        except Exception:
            description = area.get("description", f"Tutorial covering {area_name} in {self.repo_name}.")

        # Build hierarchy path: tutorial/{repo}/{focus_slug}/{area_slug}
        path_parts = ["tutorial", _slugify(self.repo_name)]
        if focus:
            path_parts.append(_slugify(focus))
        path_parts.append(_slugify(area_name))

        primary_file = (
            str(matched[0].relative_to(self.repo_path)).replace("\\", "/")
            if matched else ""
        )

        tags = ["tutorial", _slugify(self.repo_name), _slugify(area_name)]
        if focus:
            tags.append(_slugify(focus))
        # Add up to 2 stack tags from the repo analysis (passed via area if available)
        for stack_tag in area.get("_stack", [])[:2]:
            slug = _slugify(stack_tag)
            if slug not in tags:
                tags.append(slug)

        related = ", ".join(hints[:3]) if hints else "see repo structure"

        return KnowledgeEntry.create(
            source_repo=self.repo_name,
            source_file=primary_file,
            snippet_type="tutorial",
            title=f"{self.repo_name}: {area_name}",
            description=description,
            code=tutorial_md,
            tags=tags,
            context=(
                f"Part of the '{self.repo_name}' tutorial series. "
                f"Relevant paths: {related}"
            ),
            hierarchy_path="/".join(path_parts),
            source_repo_url=scanner.get_git_remote(self.repo_path),
        )

    # ── Public API ───────────────────────────────────────────────────────────

    def run(
        self,
        focus: str = "",
        areas_override: list[str] | None = None,
    ) -> int:
        """
        Run the full pipeline.

        Args:
            focus:          User's stated learning interest (free text).
            areas_override: If provided, only generate tutorials for areas with
                            these exact names (skips Ollama filtering step).

        Returns the number of new tutorial entries added to the knowledge store.
        """
        # Phase 1
        analysis = self.analyze_repo()
        stack: list[str] = analysis.get("stack", [])
        key_areas: list[dict] = analysis.get("key_areas", [])

        if not key_areas:
            self.progress_cb("No key areas identified — generating a single overview tutorial.")
            key_areas = [{
                "name": "Overview",
                "description": analysis.get("overview", ""),
                "relevant_paths": [],
            }]

        # Stamp stack onto each area so generate_tutorial_for_area can use it for tags
        for area in key_areas:
            area["_stack"] = stack

        # Phase 2
        if areas_override:
            override_set = {a.lower() for a in areas_override}
            key_areas = [a for a in key_areas if a["name"].lower() in override_set] or key_areas
        elif focus:
            key_areas = self.filter_areas_by_focus(key_areas, focus)

        self.progress_cb(
            f"Generating {len(key_areas)} tutorial(s): "
            + ", ".join(a["name"] for a in key_areas)
        )

        # Phase 3 + 4
        added = 0
        for i, area in enumerate(key_areas, 1):
            self.progress_cb(f"[{i}/{len(key_areas)}] {area['name']}")
            try:
                entry = self.generate_tutorial_for_area(area, focus)
                if entry is None:
                    continue
                if self.store.add_entry(entry):
                    added += 1
                    self.progress_cb(f"  Stored: {entry.title}")
                else:
                    self.progress_cb(f"  Already in store (duplicate content): {entry.title}")
            except _ollama.OllamaError:
                raise
            except Exception as exc:
                log.error("Skipping area '%s': %s", area.get("name"), exc)
                self.progress_cb(f"  ERROR: {exc}")

        # Register the repo in knowledge_repos so list_knowledge_sources() shows it
        self.store.upsert_repo(
            name=self.repo_name,
            local_path=str(self.repo_path),
            remote_url=scanner.get_git_remote(self.repo_path),
        )

        return added
