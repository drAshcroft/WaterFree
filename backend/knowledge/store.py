"""
Global SQLite knowledge store.

Database location: ~/.waterfree/global/knowledge.db
Schema:
  knowledge_repos  — one row per indexed source
  knowledge_entries — individual extracted snippets (FTS5 indexed)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.knowledge.models import KnowledgeEntry, KnowledgeRepo

log = logging.getLogger(__name__)

_GLOBAL_DIR = Path.home() / ".waterfree" / "global"
_DB_PATH = _GLOBAL_DIR / "knowledge.db"


def _global_db_path() -> Path:
    _GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


class KnowledgeStore:
    """Thread-safe (single-connection) SQLite store for global knowledge entries."""

    def __init__(self, db_path: Optional[str] = None):
        path = db_path or str(_global_db_path())
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_repos (
                name        TEXT PRIMARY KEY,
                local_path  TEXT NOT NULL,
                remote_url  TEXT NOT NULL DEFAULT '',
                entry_count INTEGER NOT NULL DEFAULT 0,
                last_indexed TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id           TEXT PRIMARY KEY,
                source_repo  TEXT NOT NULL,
                source_file  TEXT NOT NULL,
                snippet_type TEXT NOT NULL,
                title        TEXT NOT NULL,
                description  TEXT NOT NULL,
                code         TEXT NOT NULL,
                tags         TEXT NOT NULL DEFAULT '[]',
                content_hash TEXT NOT NULL UNIQUE,
                created_at   TEXT NOT NULL,
                source_repo_url TEXT NOT NULL DEFAULT ''
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                title,
                description,
                tags,
                code,
                content='knowledge_entries',
                content_rowid='rowid'
            );

            CREATE TRIGGER IF NOT EXISTS knowledge_fts_ai
            AFTER INSERT ON knowledge_entries BEGIN
                INSERT INTO knowledge_fts(rowid, title, description, tags, code)
                VALUES (new.rowid, new.title, new.description, new.tags, new.code);
            END;

            CREATE TRIGGER IF NOT EXISTS knowledge_fts_ad
            AFTER DELETE ON knowledge_entries BEGIN
                INSERT INTO knowledge_fts(knowledge_fts, rowid, title, description, tags, code)
                VALUES ('delete', old.rowid, old.title, old.description, old.tags, old.code);
            END;
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_entry(self, entry: KnowledgeEntry) -> bool:
        """Insert entry. Returns False (silently) if content_hash already exists."""
        try:
            self._conn.execute(
                """
                INSERT INTO knowledge_entries
                    (id, source_repo, source_file, snippet_type, title, description,
                     code, tags, content_hash, created_at, source_repo_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.source_repo,
                    entry.source_file,
                    entry.snippet_type,
                    entry.title,
                    entry.description,
                    entry.code,
                    json.dumps(entry.tags),
                    entry.content_hash,
                    entry.created_at,
                    entry.source_repo_url,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Duplicate content_hash — skip silently
            return False

    def upsert_repo(self, name: str, local_path: str, remote_url: str = "") -> None:
        count = self._entry_count_for_repo(name)
        self._conn.execute(
            """
            INSERT INTO knowledge_repos (name, local_path, remote_url, entry_count, last_indexed)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                local_path   = excluded.local_path,
                remote_url   = excluded.remote_url,
                entry_count  = excluded.entry_count,
                last_indexed = excluded.last_indexed
            """,
            (name, local_path, remote_url, count, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def _entry_count_for_repo(self, name: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM knowledge_entries WHERE source_repo = ?", (name,)
        ).fetchone()
        return row[0] if row else 0

    def delete_repo(self, name: str) -> int:
        """Delete all entries for a repo. Returns number of entries deleted."""
        cur = self._conn.execute(
            "DELETE FROM knowledge_entries WHERE source_repo = ?", (name,)
        )
        self._conn.execute("DELETE FROM knowledge_repos WHERE name = ?", (name,))
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[KnowledgeEntry]:
        """BM25-ranked FTS5 search over title + description + tags + code."""
        if not query.strip():
            return self._recent(limit)

        # Escape FTS5 special characters to avoid syntax errors
        safe_query = _escape_fts_query(query)
        try:
            rows = self._conn.execute(
                """
                SELECT e.*
                FROM knowledge_fts f
                JOIN knowledge_entries e ON e.rowid = f.rowid
                WHERE knowledge_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
            ).fetchall()
            return [_row_to_entry(r) for r in rows]
        except sqlite3.OperationalError as exc:
            log.warning("FTS search failed (%s), falling back to LIKE", exc)
            return self._fallback_search(query, limit)

    def _recent(self, limit: int) -> list[KnowledgeEntry]:
        rows = self._conn.execute(
            "SELECT * FROM knowledge_entries ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def _fallback_search(self, query: str, limit: int) -> list[KnowledgeEntry]:
        like = f"%{query}%"
        rows = self._conn.execute(
            """
            SELECT * FROM knowledge_entries
            WHERE title LIKE ? OR description LIKE ? OR tags LIKE ?
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def list_repos(self) -> list[KnowledgeRepo]:
        rows = self._conn.execute(
            "SELECT name, local_path, remote_url, entry_count, last_indexed FROM knowledge_repos"
        ).fetchall()
        return [
            KnowledgeRepo(
                name=r["name"],
                local_path=r["local_path"],
                remote_url=r["remote_url"],
                entry_count=self._entry_count_for_repo(r["name"]),
                last_indexed=r["last_indexed"],
            )
            for r in rows
        ]

    def total_entries(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        self._conn.close()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _row_to_entry(row: sqlite3.Row) -> KnowledgeEntry:
    tags_raw = row["tags"]
    try:
        tags = json.loads(tags_raw) if tags_raw else []
    except (json.JSONDecodeError, TypeError):
        tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]

    return KnowledgeEntry(
        id=row["id"],
        source_repo=row["source_repo"],
        source_file=row["source_file"],
        snippet_type=row["snippet_type"],
        title=row["title"],
        description=row["description"],
        code=row["code"],
        tags=tags,
        content_hash=row["content_hash"],
        created_at=row["created_at"],
        source_repo_url=row["source_repo_url"] or "",
    )


def _escape_fts_query(query: str) -> str:
    """
    Wrap each token in double quotes so FTS5 treats them as phrase searches,
    avoiding syntax errors from special characters like hyphens or colons.
    """
    tokens = query.strip().split()
    escaped = [f'"{t.replace(chr(34), "")}"' for t in tokens if t]
    return " OR ".join(escaped) if escaped else '""'
