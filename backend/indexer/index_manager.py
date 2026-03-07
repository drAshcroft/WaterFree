"""
IndexManager — orchestrates file parsing, code graph construction, and index persistence.
Runs parsing in a thread pool to avoid blocking.
"""

from __future__ import annotations
import json
import logging
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from backend.indexer.parser import (
    parse_file, ParsedFile, Symbol, SUPPORTED_EXTENSIONS, language_for_file
)
from backend.indexer.code_graph import CodeGraph, build_graph

log = logging.getLogger(__name__)

# Directories to skip regardless of content
DEFAULT_EXCLUDE = {
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".waterfree", ".waterfree", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    "coverage", ".next", "out",
}

MAX_FILE_SIZE_BYTES = 500_000  # skip files larger than 500KB


@dataclass
class IndexSummary:
    workspace_path: str
    file_count: int
    symbol_count: int
    language_breakdown: dict[str, int]
    top_level_modules: list[dict]   # [{"name": "...", "description": "..."}]
    entry_points: list[str]
    existing_todos: list[dict]      # [{"file": "...", "line": ..., "text": "..."}]

    def to_dict(self) -> dict:
        return {
            "workspacePath": self.workspace_path,
            "fileCount": self.file_count,
            "symbolCount": self.symbol_count,
            "languageBreakdown": self.language_breakdown,
            "topLevelModules": self.top_level_modules,
            "entryPoints": self.entry_points,
            "existingTodos": self.existing_todos,
        }

    def as_text(self) -> str:
        """Compact text representation for LLM context."""
        lang_str = ", ".join(f"{l}: {c}" for l, c in self.language_breakdown.items())
        modules_str = "\n".join(
            f"  • {m['name']}: {m['description']}" for m in self.top_level_modules
        )
        entries_str = ", ".join(self.entry_points) or "none detected"
        todos_str = f"{len(self.existing_todos)} open TODO(s)" if self.existing_todos else "no TODOs"
        return (
            f"Workspace: {self.workspace_path}\n"
            f"Files: {self.file_count} ({lang_str})\n"
            f"Symbols: {self.symbol_count}\n"
            f"Entry points: {entries_str}\n"
            f"TODOs: {todos_str}\n"
            f"Modules:\n{modules_str}"
        )


@dataclass
class IndexState:
    parsed_files: dict[str, ParsedFile] = field(default_factory=dict)  # path -> ParsedFile
    graph: Optional[CodeGraph] = None
    file_hashes: dict[str, str] = field(default_factory=dict)  # path -> sha256
    summary: Optional[IndexSummary] = None


class IndexManager:
    def __init__(
        self,
        workspace_path: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ):
        self.workspace_path = Path(workspace_path).resolve()
        self._state = IndexState()
        self._lock = threading.Lock()
        self._on_progress = on_progress
        self._index_dir = self.workspace_path / ".waterfree"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index(self, force: bool = False) -> IndexSummary:
        """
        Full workspace index. Uses cached results for unchanged files.
        Blocks until complete. Call from a worker thread in production.
        """
        files = self._collect_files()
        log.info("Indexing %d files in %s", len(files), self.workspace_path)

        self._load_cached_hashes()
        # If in-memory state is empty, always parse everything (fresh process start)
        cold_start = len(self._state.parsed_files) == 0
        to_parse = [f for f in files if force or cold_start or self._needs_reparse(f)]
        log.info("%d files need (re)parsing", len(to_parse))

        parsed: list[ParsedFile] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(parse_file, str(f)): f for f in to_parse}
            done = 0
            for future in as_completed(futures):
                result = future.result()
                parsed.append(result)
                done += 1
                if self._on_progress:
                    self._on_progress(done, len(to_parse))

        with self._lock:
            for pf in parsed:
                self._state.parsed_files[pf.path] = pf
                self._state.file_hashes[pf.path] = _file_hash(pf.path)

            all_parsed = list(self._state.parsed_files.values())
            self._state.graph = build_graph(all_parsed)
            self._state.summary = self._build_summary(all_parsed)

        self._save_index()
        return self._state.summary

    def update_file(self, path: str) -> None:
        """Re-parse a single file after it changes."""
        pf = parse_file(path)
        with self._lock:
            self._state.parsed_files[path] = pf
            self._state.file_hashes[path] = _file_hash(path)
            all_parsed = list(self._state.parsed_files.values())
            self._state.graph = build_graph(all_parsed)
            self._state.summary = self._build_summary(all_parsed)

    def remove_file(self, path: str) -> None:
        with self._lock:
            self._state.parsed_files.pop(path, None)
            self._state.file_hashes.pop(path, None)
            all_parsed = list(self._state.parsed_files.values())
            self._state.graph = build_graph(all_parsed)
            self._state.summary = self._build_summary(all_parsed)

    @property
    def summary(self) -> Optional[IndexSummary]:
        return self._state.summary

    @property
    def graph(self) -> Optional[CodeGraph]:
        return self._state.graph

    def get_symbol(self, file: str, name: str) -> Optional[Symbol]:
        pf = self._state.parsed_files.get(file)
        if not pf:
            return None
        for sym in pf.symbols:
            if sym.name == name:
                return sym
        return None

    def find_symbols_in_file(self, file: str) -> list[Symbol]:
        pf = self._state.parsed_files.get(file)
        return pf.symbols if pf else []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_files(self) -> list[Path]:
        files = []
        for path in self.workspace_path.rglob("*"):
            if path.is_dir():
                continue
            if any(part in DEFAULT_EXCLUDE for part in path.parts):
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if path.stat().st_size > MAX_FILE_SIZE_BYTES:
                continue
            files.append(path)
        return files

    def _needs_reparse(self, path: Path) -> bool:
        cached_hash = self._state.file_hashes.get(str(path))
        if not cached_hash:
            return True
        return _file_hash(str(path)) != cached_hash

    def _load_cached_hashes(self) -> None:
        meta_path = self._index_dir / "index.meta.json"
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text())
                self._state.file_hashes = data.get("hashes", {})
            except Exception:
                pass

    def _save_index(self) -> None:
        self._index_dir.mkdir(exist_ok=True)

        # Save meta (hashes)
        meta = {"hashes": self._state.file_hashes}
        (self._index_dir / "index.meta.json").write_text(json.dumps(meta, indent=2))

        # Save graph
        if self._state.graph:
            (self._index_dir / "graph.json").write_text(
                json.dumps(self._state.graph.to_dict(), indent=2)
            )

        # Save summary
        if self._state.summary:
            (self._index_dir / "index.json").write_text(
                json.dumps(self._state.summary.to_dict(), indent=2)
            )

    def _build_summary(self, parsed_files: list[ParsedFile]) -> IndexSummary:
        lang_counts: dict[str, int] = {}
        symbol_count = 0
        todos: list[dict] = []
        entry_points: list[str] = []
        modules: list[dict] = []

        for pf in parsed_files:
            if pf.error:
                continue
            lang = pf.language
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
            symbol_count += len(pf.symbols)

            # Detect entry points
            rel = str(Path(pf.path).relative_to(self.workspace_path))
            name = Path(pf.path).stem
            if name in ("main", "index", "extension", "app", "__main__"):
                entry_points.append(rel)

            # Top-level modules: directories containing source files
            parts = Path(rel).parts
            if len(parts) >= 2:
                mod_name = parts[0]
                if not any(m["name"] == mod_name for m in modules):
                    # Describe based on file names inside
                    func_names = [s.name for s in pf.symbols if s.kind == "function"][:3]
                    desc = f"{len(pf.symbols)} symbols" + (
                        f" — {', '.join(func_names)}" if func_names else ""
                    )
                    modules.append({"name": mod_name, "description": desc})

            # Scan for TODO comments
            try:
                lines = Path(pf.path).read_text(errors="replace").splitlines()
                for ln_idx, line in enumerate(lines, 1):
                    if "TODO" in line or "FIXME" in line or "HACK" in line:
                        todos.append({
                            "file": str(Path(pf.path).relative_to(self.workspace_path)),
                            "line": ln_idx,
                            "text": line.strip(),
                        })
            except OSError:
                pass

        return IndexSummary(
            workspace_path=str(self.workspace_path),
            file_count=len([pf for pf in parsed_files if not pf.error]),
            symbol_count=symbol_count,
            language_breakdown=lang_counts,
            top_level_modules=modules[:10],
            entry_points=entry_points,
            existing_todos=todos[:20],
        )


def _file_hash(path: str) -> str:
    try:
        data = Path(path).read_bytes()
        return hashlib.sha256(data).hexdigest()[:16]
    except OSError:
        return ""
