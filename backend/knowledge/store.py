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
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.knowledge.models import (
    SCOPES,
    KnowledgeEntry,
    KnowledgeRepo,
    compute_content_hash,
    default_scope,
    normalize_hierarchy_path,
)

# Search scope selectors. "default" is what a project session wants: shared
# lessons plus its own project's, never the asset catalog.
SEARCH_SCOPES = ("default", "all", "global", "project", "assets")

log = logging.getLogger(__name__)

_GLOBAL_DIR = Path.home() / ".waterfree" / "global"
_DB_PATH = _GLOBAL_DIR / "knowledge.db"


def _global_db_path() -> Path:
    _GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


_UPDATABLE_FIELDS = frozenset({
    "title", "description", "code", "snippet_type", "tags", "context",
    "source_repo", "source_file", "source_repo_url", "hierarchy_path", "scope",
})


class DuplicateContentError(ValueError):
    """An update would give an entry the same content hash as another entry."""


class KnowledgeStore:
    """Thread-safe (single-connection) SQLite store for global knowledge entries."""

    def __init__(self, db_path: Optional[str] = None):
        path = db_path or str(_global_db_path())
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._migrate()

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
                source_repo_url TEXT NOT NULL DEFAULT '',
                context      TEXT NOT NULL DEFAULT '',
                hierarchy_path TEXT NOT NULL DEFAULT ''
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

            CREATE TRIGGER IF NOT EXISTS knowledge_fts_au
            AFTER UPDATE ON knowledge_entries BEGIN
                INSERT INTO knowledge_fts(knowledge_fts, rowid, title, description, tags, code)
                VALUES ('delete', old.rowid, old.title, old.description, old.tags, old.code);
                INSERT INTO knowledge_fts(rowid, title, description, tags, code)
                VALUES (new.rowid, new.title, new.description, new.tags, new.code);
            END;
        """)
        self._conn.commit()

    def _migrate(self) -> None:
        """Add new columns to existing databases that predate schema additions."""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(knowledge_entries)")}
        if "context" not in cols:
            self._conn.execute(
                "ALTER TABLE knowledge_entries ADD COLUMN context TEXT NOT NULL DEFAULT ''"
            )
        if "hierarchy_path" not in cols:
            self._conn.execute(
                "ALTER TABLE knowledge_entries ADD COLUMN hierarchy_path TEXT NOT NULL DEFAULT ''"
            )
        if "updated_at" not in cols:
            self._conn.execute(
                "ALTER TABLE knowledge_entries ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
            )
        if "revision" not in cols:
            self._conn.execute(
                "ALTER TABLE knowledge_entries ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )
        if "scope" not in cols:
            self._conn.execute(
                "ALTER TABLE knowledge_entries ADD COLUMN scope TEXT NOT NULL DEFAULT 'global'"
            )
            self._conn.commit()
            self._backfill_scopes()
        self._conn.commit()

    def _backfill_scopes(self) -> None:
        """One-time classification of pre-scope rows.

        Asset-catalog rows are recognised by their `assets/` hierarchy. A lesson
        whose title starts with its own repo's name ("Paradoxia: shop card hit
        zone ...") was written about that project, so it becomes project-scoped;
        everything else stays global. Both rules are conservative: a wrong
        `project` only hides an entry from other repos' default searches, and
        `search --scope all` still finds it.
        """
        self._conn.execute(
            "UPDATE knowledge_entries SET scope = 'assets' "
            "WHERE hierarchy_path = 'assets' OR hierarchy_path LIKE 'assets/%'"
        )
        rows = self._conn.execute(
            "SELECT id, title, source_repo FROM knowledge_entries WHERE scope = 'global'"
        ).fetchall()
        project_ids = [
            row["id"] for row in rows
            if _repo_key(row["source_repo"]) and _title_names_repo(row["title"], row["source_repo"])
        ]
        for start in range(0, len(project_ids), 500):
            chunk = project_ids[start:start + 500]
            self._conn.execute(
                f"UPDATE knowledge_entries SET scope = 'project' WHERE id IN ({','.join('?' * len(chunk))})",
                chunk,
            )

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
                     code, tags, content_hash, created_at, source_repo_url, context, hierarchy_path, scope)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    entry.context,
                    normalize_hierarchy_path(entry.hierarchy_path),
                    default_scope(entry.hierarchy_path, entry.scope),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Duplicate content_hash — skip silently
            return False

    def update_entry(self, entry_id: str, **fields: object) -> KnowledgeEntry:
        """Revise an entry in place, keeping its id.

        Accepts any of: title, description, code, snippet_type, tags, context,
        source_repo, source_file, source_repo_url, hierarchy_path. The content
        hash is recomputed, `revision` is bumped and `updated_at` stamped.
        Raises KeyError if the id is unknown, ValueError for an unknown field,
        and DuplicateContentError if the revised body already exists elsewhere.
        """
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        current = self.get_entry(entry_id)
        if current is None:
            raise KeyError(entry_id)

        merged = {name: getattr(current, name) for name in _UPDATABLE_FIELDS}
        merged.update({k: v for k, v in fields.items() if v is not None})
        merged["hierarchy_path"] = normalize_hierarchy_path(merged["hierarchy_path"])
        merged["scope"] = default_scope(merged["hierarchy_path"], str(merged["scope"]) or None)
        tags = [str(t) for t in (merged["tags"] or [])]
        content_hash = compute_content_hash(
            str(merged["code"]),
            title=str(merged["title"]),
            description=str(merged["description"]),
            context=str(merged["context"]),
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                """
                UPDATE knowledge_entries SET
                    title = ?, description = ?, code = ?, snippet_type = ?, tags = ?,
                    context = ?, source_repo = ?, source_file = ?, source_repo_url = ?,
                    hierarchy_path = ?, scope = ?, content_hash = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ?
                """,
                (
                    merged["title"], merged["description"], merged["code"],
                    merged["snippet_type"], json.dumps(tags), merged["context"],
                    merged["source_repo"], merged["source_file"], merged["source_repo_url"],
                    merged["hierarchy_path"], merged["scope"], content_hash, updated_at, entry_id,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise DuplicateContentError(
                f"another entry already has this exact content; entry {entry_id} left unchanged"
            ) from exc
        revised = self.get_entry(entry_id)
        assert revised is not None
        return revised

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

    def delete_entry(self, entry_id: str) -> bool:
        """Delete a single knowledge entry by ID. Returns True if it existed."""
        cur = self._conn.execute(
            "DELETE FROM knowledge_entries WHERE id = ?", (entry_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

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

    def get_entry(self, entry_id: str) -> Optional[KnowledgeEntry]:
        """Fetch one entry by id, or None."""
        row = self._conn.execute(
            "SELECT * FROM knowledge_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _row_to_entry(row) if row else None

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        repo: str = "",
        prefer_repo: str = "",
        scope: str = "default",
        require_all_terms: bool = False,
    ) -> list[KnowledgeEntry]:
        """Full-text search over title + description + tags + code.

        `require_all_terms` returns only the all-terms tier: the precision gate
        for anything that injects results unasked (the prompt hook, the add-time
        duplicate check), where a weak any-term match costs more than it gives.

        `scope` selects which entries are eligible: `default` = global entries
        plus project entries whose repo is `prefer_repo` (or every project entry
        when no repo is known), never assets; `all`; or exactly one of
        `global` / `project` / `assets`.

        Ranking is precision-first: entries containing *every* query term come
        back first (BM25 order within that tier), and only when that tier is
        smaller than `limit` is it topped up with any-term matches. The old
        any-term-only ranking let one common word ("column", "add") pull in
        unrelated entries ahead of the exact match, and low-precision hits
        cost an agent more context than they return.

        `repo` restricts results to one `source_repo` (exact, case-insensitive,
        basename-tolerant). `prefer_repo` keeps the ranking but moves entries
        from that repo ahead of the rest inside each tier, so a project's own
        lessons surface before another project's.
        """
        if scope not in SEARCH_SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SEARCH_SCOPES)} (got {scope!r})")
        if not query.strip():
            return [e for e in self._recent(limit * 3) if _in_scope(e, scope, _repo_key(prefer_repo))][:limit]

        repo_key = _repo_key(repo)
        prefer_key = _repo_key(prefer_repo)
        prefer = (lambda e: 0 if _repo_key(e.source_repo) == prefer_key else 1) if prefer_key else None
        eligible = lambda e: _in_scope(e, scope, prefer_key)
        try:
            entries = self._fts_tier(_fts_query(query, "AND"), limit, repo_key, eligible)
            if prefer:
                entries.sort(key=prefer)
            if len(entries) < limit and not require_all_terms:
                seen = {entry.id for entry in entries}
                # Any-term candidates, re-ranked by how many distinct query
                # terms each one actually contains, so a three-of-four match
                # beats a one-word BM25 favourite. Preferred repo breaks ties.
                terms = _query_terms(query)
                rest = [
                    e for e in self._fts_tier(_fts_query(query, "OR"), limit * 5, repo_key, eligible)
                    if e.id not in seen
                ]
                rest.sort(key=lambda e: (
                    tuple(-n for n in _term_coverage(e, terms)),
                    prefer(e) if prefer else 0,
                ))
                entries.extend(rest[: limit - len(entries)])
        except sqlite3.OperationalError as exc:
            log.warning("FTS search failed (%s), falling back to LIKE", exc)
            entries = [e for e in self._fallback_search(query, limit * 4) if eligible(e)]
            if repo_key:
                entries = [e for e in entries if _repo_key(e.source_repo) == repo_key]
            if prefer:
                entries.sort(key=prefer)
        return entries[:limit]

    def _fts_tier(self, match: str, limit: int, repo_key: str, eligible=None) -> list[KnowledgeEntry]:
        # Over-fetch: scope and repo filters run in Python after BM25 ranking,
        # and the asset catalog alone is a fifth of the store.
        rows = self._conn.execute(
            """
            SELECT e.*
            FROM knowledge_fts f
            JOIN knowledge_entries e ON e.rowid = f.rowid
            WHERE knowledge_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match, limit * 6),
        ).fetchall()
        entries = [_row_to_entry(r) for r in rows]
        if eligible is not None:
            entries = [e for e in entries if eligible(e)]
        if repo_key:
            entries = [e for e in entries if _repo_key(e.source_repo) == repo_key]
        return entries[:limit]

    def entries_for_repo(self, repo: str) -> list[KnowledgeEntry]:
        """Every non-asset entry filed from `repo` (name or path), any scope, newest first."""
        key = _repo_key(repo)
        if not key:
            return []
        rows = self._conn.execute(
            "SELECT * FROM knowledge_entries WHERE scope != 'assets' ORDER BY created_at DESC"
        ).fetchall()
        return [e for e in (_row_to_entry(r) for r in rows) if _repo_key(e.source_repo) == key]

    def scope_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT scope, COUNT(*) AS n FROM knowledge_entries GROUP BY scope"
        ).fetchall()
        return {str(r["scope"]): int(r["n"]) for r in rows}

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

    def browse_hierarchy(
        self,
        path: str = "",
        depth: int = 2,
        include_entries: bool = False,
        entry_limit: int = 10,
    ) -> dict:
        normalized_path = normalize_hierarchy_path(path)
        root_segments = [segment for segment in normalized_path.split("/") if segment]
        entries = self._all_entries()
        subtree_entries = [
            entry for entry in entries
            if _is_in_subtree(entry.effective_hierarchy_segments(), root_segments)
        ]
        direct_entry_count = sum(
            1 for entry in subtree_entries if entry.effective_hierarchy_segments() == root_segments
        )
        result = {
            "path": normalized_path,
            "depth": max(0, depth),
            "entry_count": len(subtree_entries),
            "direct_entry_count": direct_entry_count,
            "total_entries": len(entries),
            "nodes": _build_hierarchy_nodes(subtree_entries, root_segments, max(0, depth)),
        }
        if include_entries:
            result["entries"] = [entry.to_dict() for entry in subtree_entries[:max(0, entry_limit)]]
        return result

    def close(self) -> None:
        self._conn.close()

    def _all_entries(self) -> list[KnowledgeEntry]:
        rows = self._conn.execute(
            "SELECT * FROM knowledge_entries ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_entry(r) for r in rows]


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
        context=row["context"] if "context" in row.keys() else "",
        hierarchy_path=normalize_hierarchy_path(
            row["hierarchy_path"] if "hierarchy_path" in row.keys() else ""
        ),
        updated_at=(row["updated_at"] or "") if "updated_at" in row.keys() else "",
        revision=int(row["revision"] or 1) if "revision" in row.keys() else 1,
        scope=(row["scope"] or "global") if "scope" in row.keys() else "global",
    )


def _in_scope(entry: KnowledgeEntry, scope: str, prefer_key: str) -> bool:
    if scope == "all":
        return True
    if scope in ("global", "project", "assets"):
        return entry.scope == scope
    # default: shared lessons + this project's own; other projects' private
    # notes and the asset catalog stay out unless asked for.
    if entry.scope == "global":
        return True
    if entry.scope == "project":
        return not prefer_key or _repo_key(entry.source_repo) == prefer_key
    return False


def _title_names_repo(title: str, source_repo: str) -> bool:
    key = _repo_key(source_repo)
    if not key:
        return False
    head = title.casefold().lstrip()
    for prefix in ("resolved:", "complaint:", "issue:", "fix:"):
        if head.startswith(prefix):
            head = head[len(prefix):].lstrip()
    return head.startswith(key) and (len(head) == len(key) or not head[len(key)].isalnum())


def _fts_query(query: str, joiner: str) -> str:
    """Quote every token (so hyphens/colons cannot break FTS5 syntax) and join
    them with AND or OR."""
    tokens = query.strip().split()
    escaped = [f'"{t.replace(chr(34), "")}"' for t in tokens if t]
    return f" {joiner} ".join(escaped) if escaped else '""'


def _escape_fts_query(query: str) -> str:
    """Kept for callers that still want the any-term form."""
    return _fts_query(query, "OR")


_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _query_terms(query: str) -> list[str]:
    return sorted({t for t in _TOKEN_RE.findall(query.casefold()) if len(t) > 1})


def _term_coverage(entry: KnowledgeEntry, terms: list[str]) -> tuple[int, int]:
    """(terms found in title/description/tags, terms found in code).

    The prose fields say what an entry is *about*; a long code body mentions
    hundreds of incidental identifiers, so it only breaks ties.
    """
    if not terms:
        return (0, 0)
    prose = set(_TOKEN_RE.findall(" ".join((entry.title, entry.description, " ".join(entry.tags))).casefold()))
    code = set(_TOKEN_RE.findall(entry.code.casefold()))
    return (sum(1 for t in terms if t in prose), sum(1 for t in terms if t in code))


def _repo_key(repo: str) -> str:
    """`c:/projects/Voxel_Build`, `Voxel_Build` and `voxel_build` all name one repo."""
    text = (repo or "").strip().replace("\\", "/").rstrip("/")
    if not text:
        return ""
    return text.rsplit("/", 1)[-1].casefold()


def _is_in_subtree(path_segments: list[str], root_segments: list[str]) -> bool:
    if len(root_segments) > len(path_segments):
        return False
    return path_segments[:len(root_segments)] == root_segments


def _build_hierarchy_nodes(
    entries: list[KnowledgeEntry],
    root_segments: list[str],
    depth: int,
) -> list[dict]:
    if depth <= 0:
        return []

    children: dict[str, dict] = {}
    for entry in entries:
        rel_segments = entry.effective_hierarchy_segments()[len(root_segments):]
        if not rel_segments:
            continue

        current_children = children
        traversed = list(root_segments)
        for idx, segment in enumerate(rel_segments[:depth]):
            traversed.append(segment)
            node = current_children.setdefault(
                segment,
                {
                    "name": segment,
                    "path": "/".join(traversed),
                    "entry_count": 0,
                    "direct_entry_count": 0,
                    "children": {},
                },
            )
            node["entry_count"] += 1
            if idx == len(rel_segments) - 1:
                node["direct_entry_count"] += 1
            current_children = node["children"]

    return _finalize_hierarchy_nodes(children)


def _finalize_hierarchy_nodes(children: dict[str, dict]) -> list[dict]:
    nodes = sorted(
        children.values(),
        key=lambda node: (-node["entry_count"], node["name"]),
    )
    finalized: list[dict] = []
    for node in nodes:
        finalized.append(
            {
                "name": node["name"],
                "path": node["path"],
                "entry_count": node["entry_count"],
                "direct_entry_count": node["direct_entry_count"],
                "children": _finalize_hierarchy_nodes(node["children"]),
            }
        )
    return finalized
