"""Data models for the global knowledge base."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


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
        )

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
