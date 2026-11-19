"""
Global SQLite knowledge store.

Database location: ~/.waterfree/global/knowledge.db
Schema:
  knowledge_repos  — one row per indexed source
  knowledge_entries — individual extracted snippets (FTS5 indexed)
  knowledge_entry_history — prior field values, one row per field an update
                            replaced (and a snapshot of every field on delete)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

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
# Points every KnowledgeStore() at another file. The test suite sets it
# (backend/tests/conftest.py) so no test can migrate or write the real store.
_DB_ENV = "WATERFREE_KNOWLEDGE_DB"


def _global_db_path() -> Path:
    override = os.environ.get(_DB_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    _GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


_UPDATABLE_FIELDS = frozenset({
    "title", "description", "code", "snippet_type", "tags", "context",
    "source_repo", "source_file", "source_repo_url", "hierarchy_path", "scope",
    "source_ref",
})
# Fields kept as JSON text in both knowledge_entries and the history table.
_JSON_FIELDS = frozenset({"tags", "source_ref"})

# An id prefix must be at least this long: shorter hex strings collide too
# often to be a safe handle for update/delete.
MIN_ID_PREFIX = 6
_ID_PREFIX_RE = re.compile(r"^[0-9a-f][0-9a-f-]*$")
_MAX_CANDIDATES = 10


class DuplicateContentError(ValueError):
    """An update would give an entry the same content hash as another entry."""


class AmbiguousEntryError(ValueError):
    """An id prefix or title names more than one entry.

    `candidates` holds (id, title) pairs, newest first, at most ten.
    """

    def __init__(self, message: str, candidates: list[tuple[str, str]]):
        super().__init__(message)
        self.candidates = candidates


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

            -- One row per field an update replaced: `old_value` is what the
            -- field held at `revision` (the update produced revision + 1).
            -- `change` is 'update', 'restore' or 'delete' (a delete snapshots
            -- every non-empty field so a wrong delete can be recovered by hand).
            CREATE TABLE IF NOT EXISTS knowledge_entry_history (
                seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id   TEXT NOT NULL,
                revision   INTEGER NOT NULL,
                field      TEXT NOT NULL,
                old_value  TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                change     TEXT NOT NULL DEFAULT 'update'
            );
            CREATE INDEX IF NOT EXISTS knowledge_entry_history_entry
                ON knowledge_entry_history(entry_id, revision);
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
        if "source_ref" not in cols:
            # JSON text of KnowledgeEntry.source_ref; '' when not recorded.
            self._conn.execute(
                "ALTER TABLE knowledge_entries ADD COLUMN source_ref TEXT NOT NULL DEFAULT ''"
            )
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
                     code, tags, content_hash, created_at, source_repo_url, context, hierarchy_path, scope,
                     source_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _dump_source_ref(entry.source_ref),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Duplicate content_hash — skip silently
            return False

    def update_entry(self, entry_id: str, *, change: str = "update", **fields: object) -> KnowledgeEntry:
        """Revise an entry in place, keeping its id.

        Accepts any of: title, description, code, snippet_type, tags, context,
        source_repo, source_file, source_repo_url, hierarchy_path, scope,
        source_ref. A None value leaves the field unchanged. `entry_id` may be
        a full id, an exact unique title or a unique id prefix (see
        `get_entry`). The content hash is recomputed, `revision` is bumped and
        `updated_at` stamped. The previous value of every field that actually
        changes is written to the history table in the same transaction
        (`change` labels those rows), so an overwrite can be read back with
        `history()` and undone with `restore_revision()`.
        Raises KeyError if the id is unknown, ValueError for an unknown field,
        AmbiguousEntryError for an ambiguous reference, and
        DuplicateContentError if the revised body already exists elsewhere.
        """
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        current = self.get_entry(entry_id)
        if current is None:
            raise KeyError(entry_id)
        resolved_id = current.id

        before = {name: getattr(current, name) for name in _UPDATABLE_FIELDS}
        before["hierarchy_path"] = normalize_hierarchy_path(before["hierarchy_path"])
        before["source_ref"] = before["source_ref"] or None
        merged = dict(before)
        merged.update({k: v for k, v in fields.items() if v is not None})
        merged["hierarchy_path"] = normalize_hierarchy_path(merged["hierarchy_path"])
        merged["scope"] = default_scope(merged["hierarchy_path"], str(merged["scope"]) or None)
        merged["tags"] = [str(t) for t in (merged["tags"] or [])]
        merged["source_ref"] = merged["source_ref"] or None
        content_hash = compute_content_hash(
            str(merged["code"]),
            title=str(merged["title"]),
            description=str(merged["description"]),
            context=str(merged["context"]),
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        changed = sorted(name for name in _UPDATABLE_FIELDS if merged[name] != before[name])
        try:
            self._conn.executemany(
                "INSERT INTO knowledge_entry_history (entry_id, revision, field, old_value, changed_at, change) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(resolved_id, current.revision, name, _field_text(name, before[name]), updated_at, change)
                 for name in changed],
            )
            self._conn.execute(
                """
                UPDATE knowledge_entries SET
                    title = ?, description = ?, code = ?, snippet_type = ?, tags = ?,
                    context = ?, source_repo = ?, source_file = ?, source_repo_url = ?,
                    hierarchy_path = ?, scope = ?, source_ref = ?, content_hash = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ?
                """,
                (
                    merged["title"], merged["description"], merged["code"],
                    merged["snippet_type"], json.dumps(merged["tags"]), merged["context"],
                    merged["source_repo"], merged["source_file"], merged["source_repo_url"],
                    merged["hierarchy_path"], merged["scope"], _dump_source_ref(merged["source_ref"]),
                    content_hash, updated_at, resolved_id,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise DuplicateContentError(
                f"another entry already has this exact content; entry {resolved_id} left unchanged"
            ) from exc
        revised = self.get_entry(resolved_id)
        assert revised is not None
        return revised

    def history(self, entry_ref: str) -> tuple[str, Optional[KnowledgeEntry], list[dict]]:
        """(resolved id, live entry or None if deleted, history rows newest first).

        `entry_ref` resolves like `get_entry`; when no live entry matches, a
        full id or unique id prefix of a deleted entry still finds its
        snapshot. Raises KeyError when nothing matches, and
        AmbiguousEntryError / ValueError like `get_entry`.
        """
        entry = self.get_entry(entry_ref)
        if entry is not None:
            entry_id = entry.id
        else:
            entry_id = self._resolve_history_id(entry_ref)
            if entry_id is None:
                raise KeyError(entry_ref)
        rows = self._conn.execute(
            "SELECT revision, field, old_value, changed_at, change FROM knowledge_entry_history "
            "WHERE entry_id = ? ORDER BY revision DESC, seq DESC",
            (entry_id,),
        ).fetchall()
        return entry_id, entry, [
            {
                "revision": int(r["revision"]),
                "field": r["field"],
                "old_value": r["old_value"],
                "changed_at": r["changed_at"],
                "change": r["change"],
            }
            for r in rows
        ]

    def restore_revision(self, entry_ref: str, revision: int,
                         fields: Optional[list[str]] = None) -> tuple[KnowledgeEntry, list[str]]:
        """Put fields back to the values they had at `revision`.

        The value a field had at revision N is the `old_value` of its earliest
        history row at or after N; a field with no such row has not changed
        since N. The restore is itself an update (revision bumped, the values
        it replaces written to history with change='restore'), so it can be
        undone the same way. Returns (revised entry, restored field names).
        Raises KeyError for an unknown entry and ValueError when the revision
        is out of range or nothing would change.
        """
        current = self.get_entry(entry_ref)
        if current is None:
            raise KeyError(entry_ref)
        if revision < 1 or revision >= current.revision:
            raise ValueError(
                f"--restore-revision must be between 1 and {current.revision - 1} for entry {current.id} "
                f"(its current revision is {current.revision})"
            )
        if fields:
            unknown = set(fields) - _UPDATABLE_FIELDS
            if unknown:
                raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        rows = self._conn.execute(
            "SELECT field, old_value FROM knowledge_entry_history "
            "WHERE entry_id = ? AND revision >= ? AND change != 'delete' ORDER BY revision ASC, seq ASC",
            (current.id, revision),
        ).fetchall()
        values: dict[str, object] = {}
        for row in rows:
            if row["field"] in _UPDATABLE_FIELDS:
                values.setdefault(row["field"], _field_value(row["field"], row["old_value"]))
        if fields:
            values = {k: v for k, v in values.items() if k in fields}
        current_values = {k: getattr(current, k) for k in values}
        current_values["source_ref"] = current.source_ref or None
        restore = {k: v for k, v in values.items() if v != current_values[k]}
        if not restore:
            raise ValueError(
                f"nothing to restore: no recorded change to {', '.join(fields) if fields else 'any field'} "
                f"of entry {current.id} since revision {revision}"
            )
        # update_entry reads None as "leave unchanged"; a source_ref that was
        # absent at that revision is restored as an empty mapping instead.
        if "source_ref" in restore and restore["source_ref"] is None:
            restore["source_ref"] = {}
        revised = self.update_entry(current.id, change="restore", **restore)
        return revised, sorted(restore)

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
        """Delete a single knowledge entry by its full id. Returns True if it existed.

        Every non-empty field is first copied to the history table
        (change='delete') in the same transaction, so `history(<id>)` can
        still show what a wrong delete removed.
        """
        row = self._conn.execute(
            "SELECT * FROM knowledge_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return False
        entry = _row_to_entry(row)
        stamp = datetime.now(timezone.utc).isoformat()
        self._conn.executemany(
            "INSERT INTO knowledge_entry_history (entry_id, revision, field, old_value, changed_at, change) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (entry.id, entry.revision, name, _field_text(name, getattr(entry, name)), stamp, "delete")
                for name in sorted(_UPDATABLE_FIELDS)
                if getattr(entry, name) not in ("", None, [], {})
            ],
        )
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

    def get_entry(self, entry_id: str, *, allow_title: bool = True) -> Optional[KnowledgeEntry]:
        """Fetch by full id, exact unique title, or unique id prefix; None if absent.

        Resolution order: the full id; then (when `allow_title`) an exact,
        case-insensitive title; then a hex id prefix of at least
        MIN_ID_PREFIX characters that exactly one id starts with (the 8-char
        ids agents copy from listings). Raises AmbiguousEntryError when a
        title or prefix names several entries, and ValueError when a hex
        string that matched nothing else is shorter than MIN_ID_PREFIX.
        """
        ref = (entry_id or "").strip()
        if not ref:
            return None
        row = self._conn.execute(
            "SELECT * FROM knowledge_entries WHERE id = ?", (ref,)
        ).fetchone()
        if row is not None:
            return _row_to_entry(row)
        if allow_title:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_entries WHERE title = ? COLLATE NOCASE ORDER BY created_at DESC",
                (ref,),
            ).fetchall()
            if len(rows) > 1:
                raise AmbiguousEntryError(
                    f"title {ref!r} matches {len(rows)} entries; use an entry id",
                    [(r["id"], r["title"]) for r in rows[:_MAX_CANDIDATES]],
                )
            if rows:
                return _row_to_entry(rows[0])
        prefix = ref.casefold()
        if not _ID_PREFIX_RE.match(prefix):
            return None
        if len(prefix) < MIN_ID_PREFIX:
            raise ValueError(
                f"id prefix {ref!r} is too short; give at least {MIN_ID_PREFIX} characters of the id"
            )
        rows = self._conn.execute(
            "SELECT * FROM knowledge_entries WHERE substr(id, 1, ?) = ? ORDER BY created_at DESC LIMIT ?",
            (len(prefix), prefix, _MAX_CANDIDATES + 1),
        ).fetchall()
        if len(rows) > 1:
            more = " or more" if len(rows) > _MAX_CANDIDATES else ""
            raise AmbiguousEntryError(
                f"id prefix {ref!r} matches {min(len(rows), _MAX_CANDIDATES)}{more} entries; "
                "give more characters of the id",
                [(r["id"], r["title"]) for r in rows[:_MAX_CANDIDATES]],
            )
        return _row_to_entry(rows[0]) if rows else None

    def _resolve_history_id(self, ref: str) -> Optional[str]:
        """A deleted entry's id from its full id or a unique prefix, via its history rows."""
        prefix = (ref or "").strip().casefold()
        if not _ID_PREFIX_RE.match(prefix) or len(prefix) < MIN_ID_PREFIX:
            return None
        rows = self._conn.execute(
            "SELECT DISTINCT entry_id FROM knowledge_entry_history WHERE substr(entry_id, 1, ?) = ? LIMIT ?",
            (len(prefix), prefix, _MAX_CANDIDATES + 1),
        ).fetchall()
        if len(rows) > 1:
            raise AmbiguousEntryError(
                f"id prefix {ref!r} matches {len(rows)} deleted entries; give more characters of the id",
                [(r["entry_id"], "(deleted)") for r in rows[:_MAX_CANDIDATES]],
            )
        return rows[0]["entry_id"] if rows else None

    def has_content_hash(self, content_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM knowledge_entries WHERE content_hash = ? LIMIT 1", (content_hash,)
        ).fetchone()
        return row is not None

    def get_by_content_hash(self, content_hash: str) -> Optional[KnowledgeEntry]:
        """The entry that already holds this exact content, if any."""
        row = self._conn.execute(
            "SELECT * FROM knowledge_entries WHERE content_hash = ? LIMIT 1", (content_hash,)
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
        path: str = "",
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
        lessons surface before another project's. The preferred repo's
        all-terms matches are fetched on their own before the tier is cut to
        `limit`, so a crowd of better-scoring entries from other repos can
        never push them out (they used to be truncated in BM25 order first and
        only then sorted, which dropped them).
        """
        if scope not in SEARCH_SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SEARCH_SCOPES)} (got {scope!r})")
        path_key = normalize_hierarchy_path(path)
        if not query.strip():
            return [e for e in self._recent(limit * 10)
                    if _in_scope(e, scope, _repo_key(prefer_repo))
                    and _is_in_subtree(e.effective_hierarchy_segments(), path_key.split("/") if path_key else [])][:limit]

        repo_key = _repo_key(repo)
        prefer_key = _repo_key(prefer_repo)
        prefer = (lambda e: 0 if _repo_key(e.source_repo) == prefer_key else 1) if prefer_key else None
        eligible = lambda e: (_in_scope(e, scope, prefer_key) and
                              _is_in_subtree(e.effective_hierarchy_segments(), path_key.split("/") if path_key else []))
        # With --repo every row already comes from one repo; otherwise the
        # preferred repo's matches get a dedicated, repo-filtered fetch.
        own_key = prefer_key if prefer_key and not repo_key else ""
        try:
            match = _fts_query(query, "AND")
            entries = self._fts_tier(match, limit, repo_key, eligible, path_key)
            if own_key:
                entries = _own_first(self._fts_tier(match, limit, own_key, eligible, path_key), entries)
            if prefer:
                entries.sort(key=prefer)  # stable: BM25 order within each group
            entries = entries[:limit]
            if len(entries) < limit and not require_all_terms:
                seen = {entry.id for entry in entries}
                # Any-term candidates, re-ranked by how many distinct query
                # terms each one actually contains, so a three-of-four match
                # beats a one-word BM25 favourite. Preferred repo breaks ties.
                terms = _query_terms(query)
                rest = [
                    e for e in self._fts_tier(_fts_query(query, "OR"), limit * 5, repo_key, eligible, path_key)
                    if e.id not in seen
                ]
                rest.sort(key=lambda e: (
                    tuple(-n for n in _term_coverage(e, terms)),
                    prefer(e) if prefer else 0,
                ))
                entries.extend(rest[: limit - len(entries)])
        except sqlite3.OperationalError as exc:
            log.warning("FTS search failed (%s), falling back to LIKE", exc)
            entries = [e for e in self._fallback_search(query, limit * 4, repo_key) if eligible(e)]
            if repo_key:
                entries = [e for e in entries if _repo_key(e.source_repo) == repo_key]
            if own_key:
                own = [e for e in self._fallback_search(query, limit * 4, own_key)
                       if eligible(e) and _repo_key(e.source_repo) == own_key]
                entries = _own_first(own, entries)
            if prefer:
                entries.sort(key=prefer)
        return entries[:limit]

    def _fts_tier(self, match: str, limit: int, repo_key: str, eligible=None,
                  path_key: str = "") -> list[KnowledgeEntry]:
        # Over-fetch: scope filters run in Python after BM25 ranking, and the
        # asset catalog alone is a fifth of the store. A repo filter is also
        # pushed into SQL (as a suffix pre-filter, re-checked exactly below)
        # so a repo's matches are not lost behind other repos' rows.
        path_clause = ""
        params: list[object] = [match]
        if path_key:
            path_clause = "AND (lower(e.hierarchy_path) = ? OR substr(lower(e.hierarchy_path), 1, length(?) + 1) = ? || '/')"
            params.extend([path_key, path_key, path_key])
        repo_clause = ""
        if repo_key:
            repo_clause = f"AND {_REPO_SQL} LIKE ? ESCAPE '\\'"
            params.append(_repo_like(repo_key))
        params.append(limit * 6)
        rows = self._conn.execute(
            f"""
            SELECT e.*
            FROM knowledge_fts f
            JOIN knowledge_entries e ON e.rowid = f.rowid
            WHERE knowledge_fts MATCH ?
            {path_clause}
            {repo_clause}
            ORDER BY rank
            LIMIT ?
            """,
            params,
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

    def _fallback_search(self, query: str, limit: int, repo_key: str = "") -> list[KnowledgeEntry]:
        like = f"%{query}%"
        params: list[object] = [like, like, like]
        repo_clause = ""
        if repo_key:
            repo_clause = f"AND {_REPO_SQL.replace('e.', '')} LIKE ? ESCAPE '\\'"
            params.append(_repo_like(repo_key))
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT * FROM knowledge_entries
            WHERE (title LIKE ? OR description LIKE ? OR tags LIKE ?)
            {repo_clause}
            LIMIT ?
            """,
            params,
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
        list_entries: bool = False,
        entry_formatter: Optional[Callable[[KnowledgeEntry], dict]] = None,
    ) -> dict:
        """Child nodes and counts under `path`; optionally the branch's entries.

        Entry rows are `entry.to_dict()` unless `entry_formatter` shapes them
        (the CLI passes its compact search-row shape).
        """
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
        if include_entries or list_entries:
            shape = entry_formatter or (lambda entry: entry.to_dict())
            result["entries"] = [shape(entry) for entry in subtree_entries[:max(0, entry_limit)]]
            result["entries_truncated"] = len(subtree_entries) > max(0, entry_limit)
        return result

    def hierarchy_node_counts(self) -> dict[str, int]:
        """Every taxonomy node path (each prefix of each entry's path) -> subtree entry count."""
        counts: dict[str, int] = {}
        rows = self._conn.execute(
            "SELECT hierarchy_path, snippet_type, tags FROM knowledge_entries"
        ).fetchall()
        for row in rows:
            entry = KnowledgeEntry(
                id="", source_repo="", source_file="", snippet_type=row["snippet_type"], title="",
                description="", code="", tags=_decode_tags(row["tags"]), content_hash="", created_at="",
                hierarchy_path=normalize_hierarchy_path(row["hierarchy_path"]),
            )
            segments = entry.effective_hierarchy_segments()
            for end in range(1, len(segments) + 1):
                node = "/".join(segments[:end])
                counts[node] = counts.get(node, 0) + 1
        return counts

    def nearest_hierarchy_paths(self, path: str, *, limit: int = 5) -> list[dict]:
        """Existing node paths an agent probably meant by `path`, best first.

        Matches ignore case, whitespace and separators ('Procedural buildings',
        'procedural \\\\ buildings' and 'procedural/buildings' are one key), then
        fall back to a trailing-segment match and a small edit distance. The
        exact node itself is never returned. Rows: {path, entry_count, match}.
        """
        wanted = normalize_hierarchy_path(path)
        if not wanted:
            return []
        return _nearest_paths(wanted, self.hierarchy_node_counts(), limit=limit)

    def separator_twins(self, path: str) -> list[dict]:
        """Other nodes spelling the same words with different separators
        ('procedural buildings' vs 'procedural/buildings'). Rows: {path, entry_count, match}."""
        wanted = normalize_hierarchy_path(path)
        key = _path_key(wanted)
        if not key:
            return []
        twins = [(node, count) for node, count in self.hierarchy_node_counts().items()
                 if node != wanted and _path_key(node) == key]
        twins.sort(key=lambda item: (-item[1], item[0]))
        return [{"path": node, "entry_count": count, "match": _SAME_WORDS} for node, count in twins]

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

def _decode_tags(tags_raw: object) -> list[str]:
    try:
        tags = json.loads(tags_raw) if tags_raw else []
    except (json.JSONDecodeError, TypeError):
        tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]
    return tags if isinstance(tags, list) else []


def _dump_source_ref(source_ref: object) -> str:
    return json.dumps(source_ref, sort_keys=True) if source_ref else ""


def _load_source_ref(raw: object) -> Optional[dict]:
    if not raw:
        return None
    try:
        value = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) and value else None


def _field_text(name: str, value: object) -> str:
    """How a field's value is stored in the history table."""
    if name == "tags":
        return json.dumps([str(t) for t in (value or [])])
    if name == "source_ref":
        return _dump_source_ref(value)
    return "" if value is None else str(value)


def _field_value(name: str, text: str) -> object:
    """Inverse of _field_text."""
    if name == "tags":
        return _decode_tags(text)
    if name == "source_ref":
        return _load_source_ref(text)
    return text


def _own_first(own: list[KnowledgeEntry], rest: list[KnowledgeEntry]) -> list[KnowledgeEntry]:
    seen = {e.id for e in own}
    return own + [e for e in rest if e.id not in seen]


# SQL twin of _repo_key's normalisation (separators, trailing slash; LIKE is
# ASCII case-insensitive). Used only as a pre-filter: rows are re-checked
# with _repo_key in Python, so a suffix such as 'mydungeon' for 'dungeon'
# costs nothing but a discarded row.
_REPO_SQL = "rtrim(replace(e.source_repo, '\\', '/'), '/')"


def _repo_like(repo_key: str) -> str:
    escaped = repo_key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}"


def _row_to_entry(row: sqlite3.Row) -> KnowledgeEntry:
    tags = _decode_tags(row["tags"])
    keys = row.keys()

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
        context=row["context"] if "context" in keys else "",
        hierarchy_path=normalize_hierarchy_path(
            row["hierarchy_path"] if "hierarchy_path" in keys else ""
        ),
        updated_at=(row["updated_at"] or "") if "updated_at" in keys else "",
        revision=int(row["revision"] or 1) if "revision" in keys else 1,
        scope=(row["scope"] or "global") if "scope" in keys else "global",
        source_ref=_load_source_ref(row["source_ref"]) if "source_ref" in keys else None,
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


_SAME_WORDS = "same words, different separators"
_PATH_KEY_STRIP = re.compile(r"[\s/\\_\-.:]+")


def _path_key(path: str) -> str:
    """'Procedural buildings', 'procedural/buildings' and 'procedural_buildings' share one key."""
    return _PATH_KEY_STRIP.sub("", path.casefold())


def _edit_distance(a: str, b: str, cap: int) -> int:
    """Levenshtein distance, giving up (returning cap + 1) once it must exceed `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > cap:
            return cap + 1
        previous = current
    return previous[-1]


def _nearest_paths(wanted: str, counts: dict[str, int], *, limit: int) -> list[dict]:
    """Rank node paths by how likely they are what `wanted` meant.

    Tiers, best first: same key once case/space/separators are ignored; the
    wanted path is the tail of a deeper node ('buildings/houses' ->
    'procedural/buildings/houses'); same last segment; small edit distance on
    the key (typos); one key contains the other. Within a tier, bigger
    subtrees first.
    """
    key = _path_key(wanted)
    if not key:
        return []
    wanted_segments = wanted.split("/")
    wanted_last = _path_key(wanted_segments[-1])
    cap = max(1, len(key) // 5)
    ranked: list[tuple[int, int, int, str, str]] = []
    for node, count in counts.items():
        if node == wanted:
            continue
        node_key = _path_key(node)
        segments = node.split("/")
        tail_key = _path_key("/".join(segments[-len(wanted_segments):])) if len(segments) >= len(wanted_segments) else ""
        if node_key == key:
            tier, how = 0, _SAME_WORDS
        elif len(segments) > len(wanted_segments) and tail_key == key:
            tier, how = 1, "deeper path ending in the same segments"
        elif wanted_last and _path_key(segments[-1]) == wanted_last:
            tier, how = 2, "same last segment"
        else:
            last_key = _path_key(segments[-1])
            last_cap = max(1, len(wanted_last) // 5)
            if (_edit_distance(key, node_key, cap) <= cap
                    or (wanted_last and _edit_distance(wanted_last, last_key, last_cap) <= last_cap)):
                tier, how = 3, "spelled similarly"
            elif len(key) >= 4 and (key in node_key or (len(node_key) >= 4 and node_key in key)):
                tier, how = 4, "contains the same words"
            else:
                continue
        ranked.append((tier, -count, len(node), node, how))
    ranked.sort()
    return [{"path": node, "entry_count": -neg, "match": how} for _, neg, _, node, how in ranked[:limit]]


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
