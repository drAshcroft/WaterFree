"""
SQLite-backed graph storage for the internal codebase indexer.

Schema mirrors codebase-memory-mcp's design:
  projects, nodes, edges, file_hashes, project_summaries
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
    name     TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    label          TEXT NOT NULL,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path      TEXT,
    start_line     INTEGER DEFAULT 0,
    end_line       INTEGER DEFAULT 0,
    properties     TEXT DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_qn
    ON nodes(project, qualified_name);
CREATE INDEX IF NOT EXISTS idx_nodes_name  ON nodes(project, name);
CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(project, label);
CREATE INDEX IF NOT EXISTS idx_nodes_file  ON nodes(project, file_path);

CREATE TABLE IF NOT EXISTS edges (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    project   TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    type      TEXT NOT NULL,
    properties TEXT DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_unique
    ON edges(source_id, target_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(project, source_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_tgt ON edges(project, target_id, type);

CREATE TABLE IF NOT EXISTS file_hashes (
    project  TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    sha256   TEXT NOT NULL,
    PRIMARY KEY (project, rel_path)
);

CREATE TABLE IF NOT EXISTS project_summaries (
    project    TEXT PRIMARY KEY REFERENCES projects(name) ON DELETE CASCADE,
    summary    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


class GraphStore:
    """Thread-safe SQLite wrapper for graph data."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Apply schema on a fresh connection
        con = self._connect()
        con.executescript(_SCHEMA)
        con.commit()

    # ------------------------------------------------------------------
    # Connection management (one connection per thread)
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if not getattr(self._local, "con", None):
            con = sqlite3.connect(self._db_path, check_same_thread=False)
            con.row_factory = sqlite3.Row
            self._local.con = con
        return self._local.con

    @property
    def _con(self) -> sqlite3.Connection:
        return self._connect()

    @property
    def db_path(self) -> str:
        return self._db_path

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def upsert_project(self, name: str, root_path: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            "INSERT INTO projects(name, root_path, indexed_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET root_path=excluded.root_path, "
            "indexed_at=excluded.indexed_at",
            (name, root_path, now),
        )

    def list_projects(self) -> list[dict]:
        rows = self._con.execute(
            "SELECT p.name, p.root_path, p.indexed_at, "
            "  (SELECT COUNT(*) FROM nodes WHERE project=p.name) AS node_count, "
            "  (SELECT COUNT(*) FROM edges WHERE project=p.name) AS edge_count "
            "FROM projects p"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_project(self, name: str) -> Optional[dict]:
        row = self._con.execute(
            "SELECT * FROM projects WHERE name=?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def get_project_by_root(self, root_path: str) -> Optional[dict]:
        row = self._con.execute(
            "SELECT * FROM projects WHERE root_path=?", (root_path,)
        ).fetchone()
        return dict(row) if row else None

    def delete_project(self, name: str) -> None:
        self._con.execute("DELETE FROM projects WHERE name=?", (name,))

    def project_count(self) -> int:
        row = self._con.execute("SELECT COUNT(*) FROM projects").fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def upsert_node(
        self,
        project: str,
        label: str,
        name: str,
        qualified_name: str,
        file_path: str = "",
        start_line: int = 0,
        end_line: int = 0,
        properties: dict | None = None,
    ) -> int:
        """Insert or replace a node. Returns its id."""
        props = json.dumps(properties or {})
        cur = self._con.execute(
            "INSERT INTO nodes(project,label,name,qualified_name,file_path,"
            "start_line,end_line,properties) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project,qualified_name) DO UPDATE SET "
            "label=excluded.label, name=excluded.name, file_path=excluded.file_path, "
            "start_line=excluded.start_line, end_line=excluded.end_line, "
            "properties=excluded.properties "
            "RETURNING id",
            (project, label, name, qualified_name, file_path,
             start_line, end_line, props),
        )
        row = cur.fetchone()
        return row[0] if row else self._get_node_id(project, qualified_name)

    def _get_node_id(self, project: str, qualified_name: str) -> int:
        row = self._con.execute(
            "SELECT id FROM nodes WHERE project=? AND qualified_name=?",
            (project, qualified_name),
        ).fetchone()
        return row[0] if row else -1

    def get_node_by_qn(self, project: str, qualified_name: str) -> Optional[dict]:
        row = self._con.execute(
            "SELECT * FROM nodes WHERE project=? AND qualified_name=?",
            (project, qualified_name),
        ).fetchone()
        return dict(row) if row else None

    def get_node_by_id(self, node_id: int) -> Optional[dict]:
        row = self._con.execute(
            "SELECT * FROM nodes WHERE id=?", (node_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_nodes_by_name(self, project: str, name: str, label: str = "") -> list[dict]:
        if label:
            rows = self._con.execute(
                "SELECT * FROM nodes WHERE project=? AND name=? AND label=?",
                (project, name, label),
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT * FROM nodes WHERE project=? AND name=?",
                (project, name),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_nodes(
        self,
        project: str,
        pattern: str,
        label: str = "",
        limit: int = 0,
        case_sensitive: bool = False,
    ) -> list[dict]:
        """Search nodes whose name matches a LIKE pattern."""
        like = f"%{pattern}%"
        limit_sql = ""
        params: list[object] = [project, like]
        if label:
            params.append(label)
        if limit > 0:
            limit_sql = " LIMIT ?"
            params.append(limit)

        if case_sensitive:
            if label:
                rows = self._con.execute(
                    "SELECT * FROM nodes WHERE project=? AND name LIKE ? ESCAPE '\\' "
                    f"AND label=?{limit_sql}",
                    tuple(params),
                ).fetchall()
            else:
                rows = self._con.execute(
                    f"SELECT * FROM nodes WHERE project=? AND name LIKE ? ESCAPE '\\'{limit_sql}",
                    tuple(params),
                ).fetchall()
        else:
            if label:
                rows = self._con.execute(
                    "SELECT * FROM nodes WHERE project=? "
                    f"AND LOWER(name) LIKE LOWER(?) AND label=?{limit_sql}",
                    tuple(params),
                ).fetchall()
            else:
                rows = self._con.execute(
                    f"SELECT * FROM nodes WHERE project=? AND LOWER(name) LIKE LOWER(?){limit_sql}",
                    tuple(params),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_all_nodes(self, project: str, label: str = "") -> list[dict]:
        if label:
            rows = self._con.execute(
                "SELECT * FROM nodes WHERE project=? AND label=?", (project, label)
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT * FROM nodes WHERE project=?", (project,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_nodes_for_file(self, project: str, file_path: str) -> None:
        self._con.execute(
            "DELETE FROM nodes WHERE project=? AND file_path=?", (project, file_path)
        )

    # ------------------------------------------------------------------
    # Edges
    # ------------------------------------------------------------------

    def upsert_edge(
        self,
        project: str,
        source_id: int,
        target_id: int,
        edge_type: str,
        properties: dict | None = None,
    ) -> None:
        props = json.dumps(properties or {})
        self._con.execute(
            "INSERT INTO edges(project,source_id,target_id,type,properties) VALUES(?,?,?,?,?) "
            "ON CONFLICT(source_id,target_id,type) DO UPDATE SET properties=excluded.properties",
            (project, source_id, target_id, edge_type, props),
        )

    def get_outbound_edges(self, project: str, source_id: int, edge_type: str = "") -> list[dict]:
        if edge_type:
            rows = self._con.execute(
                "SELECT e.*, n.name, n.qualified_name, n.label, n.file_path, "
                "n.start_line, n.end_line, n.properties as node_props "
                "FROM edges e JOIN nodes n ON n.id=e.target_id "
                "WHERE e.project=? AND e.source_id=? AND e.type=?",
                (project, source_id, edge_type),
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT e.*, n.name, n.qualified_name, n.label, n.file_path, "
                "n.start_line, n.end_line, n.properties as node_props "
                "FROM edges e JOIN nodes n ON n.id=e.target_id "
                "WHERE e.project=? AND e.source_id=?",
                (project, source_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_inbound_edges(self, project: str, target_id: int, edge_type: str = "") -> list[dict]:
        if edge_type:
            rows = self._con.execute(
                "SELECT e.*, n.name, n.qualified_name, n.label, n.file_path, "
                "n.start_line, n.end_line, n.properties as node_props "
                "FROM edges e JOIN nodes n ON n.id=e.source_id "
                "WHERE e.project=? AND e.target_id=? AND e.type=?",
                (project, target_id, edge_type),
            ).fetchall()
        else:
            rows = self._con.execute(
                "SELECT e.*, n.name, n.qualified_name, n.label, n.file_path, "
                "n.start_line, n.end_line, n.properties as node_props "
                "FROM edges e JOIN nodes n ON n.id=e.source_id "
                "WHERE e.project=? AND e.target_id=?",
                (project, target_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_in_degree(self, node_id: int, edge_type: str = "CALLS") -> int:
        row = self._con.execute(
            "SELECT COUNT(*) FROM edges WHERE target_id=? AND type=?",
            (node_id, edge_type),
        ).fetchone()
        return row[0] if row else 0

    def get_out_degree(self, node_id: int, edge_type: str = "CALLS") -> int:
        row = self._con.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id=? AND type=?",
            (node_id, edge_type),
        ).fetchone()
        return row[0] if row else 0

    def delete_edges_for_file(self, project: str, file_path: str) -> None:
        """Delete all edges where source node is in the given file."""
        self._con.execute(
            "DELETE FROM edges WHERE source_id IN "
            "(SELECT id FROM nodes WHERE project=? AND file_path=?)",
            (project, file_path),
        )

    def count_edges(self, project: str, edge_type: str = "") -> int:
        if edge_type:
            row = self._con.execute(
                "SELECT COUNT(*) FROM edges WHERE project=? AND type=?",
                (project, edge_type),
            ).fetchone()
        else:
            row = self._con.execute(
                "SELECT COUNT(*) FROM edges WHERE project=?",
                (project,),
            ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # File hashes
    # ------------------------------------------------------------------

    def get_file_hashes(self, project: str) -> dict[str, str]:
        rows = self._con.execute(
            "SELECT rel_path, sha256 FROM file_hashes WHERE project=?", (project,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def upsert_file_hash(self, project: str, rel_path: str, sha256: str) -> None:
        self._con.execute(
            "INSERT INTO file_hashes(project, rel_path, sha256) VALUES(?,?,?) "
            "ON CONFLICT(project, rel_path) DO UPDATE SET sha256=excluded.sha256",
            (project, rel_path, sha256),
        )

    def delete_file_hash(self, project: str, rel_path: str) -> None:
        self._con.execute(
            "DELETE FROM file_hashes WHERE project=? AND rel_path=?", (project, rel_path)
        )

    def get_node_label_counts(self, project: str) -> list[dict]:
        rows = self._con.execute(
            "SELECT label, COUNT(*) AS count "
            "FROM nodes WHERE project=? "
            "GROUP BY label ORDER BY count DESC, label ASC",
            (project,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_edge_type_counts(self, project: str) -> list[dict]:
        rows = self._con.execute(
            "SELECT type, COUNT(*) AS count "
            "FROM edges WHERE project=? "
            "GROUP BY type ORDER BY count DESC, type ASC",
            (project,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_relationship_patterns(self, project: str, limit: int = 20) -> list[dict]:
        rows = self._con.execute(
            "SELECT src.label AS source_label, e.type AS edge_type, tgt.label AS target_label, "
            "COUNT(*) AS count "
            "FROM edges e "
            "JOIN nodes src ON src.id = e.source_id "
            "JOIN nodes tgt ON tgt.id = e.target_id "
            "WHERE e.project=? "
            "GROUP BY src.label, e.type, tgt.label "
            "ORDER BY count DESC, source_label ASC, edge_type ASC, target_label ASC "
            "LIMIT ?",
            (project, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_sample_nodes(self, project: str, limit_per_label: int = 3) -> list[dict]:
        rows = self._con.execute(
            "SELECT label, name, qualified_name "
            "FROM nodes WHERE project=? "
            "ORDER BY label ASC, name ASC",
            (project,),
        ).fetchall()

        samples: list[dict] = []
        per_label: dict[str, int] = {}
        for row in rows:
            label = row["label"]
            if per_label.get(label, 0) >= limit_per_label:
                continue
            per_label[label] = per_label.get(label, 0) + 1
            samples.append(dict(row))
        return samples

    # ------------------------------------------------------------------
    # ADR / project summaries
    # ------------------------------------------------------------------

    def get_summary(self, project: str) -> Optional[str]:
        row = self._con.execute(
            "SELECT summary FROM project_summaries WHERE project=?", (project,)
        ).fetchone()
        return row[0] if row else None

    def upsert_summary(self, project: str, summary: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._con.execute(
            "INSERT INTO project_summaries(project, summary, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(project) DO UPDATE SET summary=excluded.summary, "
            "updated_at=excluded.updated_at",
            (project, summary, now),
        )

    # ------------------------------------------------------------------
    # Transaction helpers
    # ------------------------------------------------------------------

    def commit(self) -> None:
        self._con.commit()

    def rollback(self) -> None:
        self._con.rollback()

    def close(self) -> None:
        if getattr(self._local, "con", None):
            self._local.con.close()
            self._local.con = None

    def delete_db_file(self) -> None:
        self.close()
        try:
            os.remove(self._db_path)
        except FileNotFoundError:
            return
