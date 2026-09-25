"""Data models for the global knowledge base."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence


def _normalize_hierarchy_segment(segment: object) -> str:
    text = " ".join(str(segment).replace("\\", "/").strip().split())
    return text.casefold()


def normalize_hierarchy_path(path: str | Sequence[object] | None) -> str:
    if path is None:
        return ""

    if isinstance(path, str):
        raw_segments = path.replace("\\", "/").split("/")
    else:
        raw_segments = list(path)

    segments: list[str] = []
    for segment in raw_segments:
        normalized = _normalize_hierarchy_segment(segment)
        if normalized and normalized != ".":
            segments.append(normalized)
    return "/".join(segments)


def compute_content_hash(code: str, *, title: str = "", description: str = "", context: str = "") -> str:
    """Dedup key for an entry.

    Entries with code hash the code alone, so two write-ups of the same snippet
    collapse into one and hashes of existing rows stay valid. A prose-only
    entry (a lesson, a convention, a decision) has no code to hash, so it is
    keyed on its title, description and context instead; otherwise every
    code-less entry would collide with the first one ever added.
    """
    if code.strip():
        return hashlib.sha256(code.encode()).hexdigest()
    prose = "\0".join(("prose", title.strip(), description.strip(), context.strip()))
    return hashlib.sha256(prose.encode()).hexdigest()


SCOPES = ("global", "project", "assets")


def default_scope(hierarchy_path: str, scope: str | None = None) -> str:
    """`assets/...` rows are catalog entries; everything else is global unless told otherwise."""
    if scope:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)} (got {scope!r})")
        return scope
    first = normalize_hierarchy_path(hierarchy_path).split("/", 1)[0]
    return "assets" if first == "assets" else "global"


@dataclass
class KnowledgeEntry:
    """A single extracted snippet stored in the global knowledge base.

    `code` may be empty for prose-only entries (lessons, conventions, decisions);
    the description and context then carry the whole entry.

    `scope` says who a search should show the entry to: `global` (any project),
    `project` (only searches run from its own `source_repo`), or `assets`
    (the owned-asset catalog, only on request). Sharing one BM25 index between
    a licensing row for a Unity pack and a cross-project convention was how
    unrelated hits crowded out the exact match.
    """

    id: str
    source_repo: str           # short name of the source project / repo
    source_file: str           # relative path within that repo
    snippet_type: str          # "pattern" | "utility" | "style" | "api_usage" | "convention"
    title: str                 # LLM-generated short title
    description: str           # LLM-generated plain-English summary
    code: str                  # raw source code
    tags: list[str]            # LLM-extracted tags, e.g. ["python", "django", "auth"]
    content_hash: str          # SHA-256 of code — used for dedup
    created_at: str            # ISO-8601 timestamp
    source_repo_url: str = ""  # git remote URL (optional)
    context: str = ""          # caveats, dependencies, related files, when NOT to use
    hierarchy_path: str = ""   # explicit taxonomy path, e.g. "backend/auth/jwt"
    updated_at: str = ""       # ISO-8601 timestamp of the last in-place revision ("" if never)
    revision: int = 1          # bumped by every in-place update; the id never changes
    scope: str = "global"      # "global" | "project" | "assets"; see class docstring

    @classmethod
    def create(
        cls,
        source_repo: str,
        source_file: str,
        snippet_type: str,
        title: str,
        description: str,
        code: str,
        tags: list[str],
        source_repo_url: str = "",
        context: str = "",
        hierarchy_path: str | Sequence[object] | None = None,
        scope: str | None = None,
    ) -> "KnowledgeEntry":
        normalized_path = normalize_hierarchy_path(hierarchy_path)
        return cls(
            id=str(uuid.uuid4()),
            source_repo=source_repo,
            source_file=source_file,
            snippet_type=snippet_type,
            title=title,
            description=description,
            code=code,
            tags=tags,
            content_hash=compute_content_hash(
                code, title=title, description=description, context=context
            ),
            created_at=datetime.now(timezone.utc).isoformat(),
            source_repo_url=source_repo_url,
            context=context,
            hierarchy_path=normalized_path,
            scope=default_scope(normalized_path, scope),
        )

    def hierarchy_segments(self) -> list[str]:
        return [segment for segment in self.hierarchy_path.split("/") if segment]

    def effective_hierarchy_segments(self) -> list[str]:
        explicit = self.hierarchy_segments()
        if explicit:
            return explicit

        derived: list[str] = []
        snippet_segment = _normalize_hierarchy_segment(self.snippet_type)
        if snippet_segment:
            derived.append(snippet_segment)

        for tag in self.tags:
            tag_segment = _normalize_hierarchy_segment(tag)
            if tag_segment and tag_segment not in derived:
                derived.append(tag_segment)
            if len(derived) >= 4:
                break

        return derived

    def effective_hierarchy_path(self) -> str:
        return "/".join(self.effective_hierarchy_segments())

    def hierarchy_source(self) -> str:
        return "explicit" if self.hierarchy_path else "derived"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sourceRepo": self.source_repo,
            "sourceFile": self.source_file,
            "snippetType": self.snippet_type,
            "title": self.title,
            "description": self.description,
            "code": self.code,
            "tags": self.tags,
            "contentHash": self.content_hash,
            "createdAt": self.created_at,
            "sourceRepoUrl": self.source_repo_url,
            "context": self.context,
            "hierarchyPath": self.effective_hierarchy_path(),
            "hierarchySegments": self.effective_hierarchy_segments(),
            "hierarchySource": self.hierarchy_source(),
            "updatedAt": self.updated_at,
            "revision": self.revision,
            "scope": self.scope,
        }


@dataclass
class KnowledgeRepo:
    """Metadata about an indexed knowledge source."""

    name: str
    local_path: str
    remote_url: str
    entry_count: int
    last_indexed: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "localPath": self.local_path,
            "remoteUrl": self.remote_url,
            "entryCount": self.entry_count,
            "lastIndexed": self.last_indexed,
        }
