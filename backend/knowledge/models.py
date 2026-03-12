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


@dataclass
class KnowledgeEntry:
    """A single extracted snippet stored in the global knowledge base."""

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
    ) -> "KnowledgeEntry":
        return cls(
            id=str(uuid.uuid4()),
            source_repo=source_repo,
            source_file=source_file,
            snippet_type=snippet_type,
            title=title,
            description=description,
            code=code,
            tags=tags,
            content_hash=hashlib.sha256(code.encode()).hexdigest(),
            created_at=datetime.now(timezone.utc).isoformat(),
            source_repo_url=source_repo_url,
            context=context,
            hierarchy_path=normalize_hierarchy_path(hierarchy_path),
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
