"""
Git repo importer — ingests a local or remote git repo into the global knowledge base.

Workflow:
  1. If URL given, clone to ~/.waterfree/global/repos/<name>/
  2. Use the existing graph/indexer.py to extract AST symbols (tree-sitter)
  3. Pass symbols to KnowledgeExtractor for LLM classification
  4. Store accepted entries in KnowledgeStore
  5. Update knowledge_repos metadata row
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

from backend.knowledge.extractor import KnowledgeExtractor
from backend.knowledge.store import KnowledgeStore
from backend.llm.runtime_registry import choose_runtime_for_stage, create_runtime

log = logging.getLogger(__name__)

_REPOS_DIR = Path.home() / ".waterfree" / "global" / "repos"


def import_repo(
    source: str,
    store: KnowledgeStore,
    runtime=None,
    focus: str = "",
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """
    Import a repo (local path or git URL) into the global knowledge store.

    focus: optional user description of what to extract (e.g. "auth patterns").
    Returns a summary dict: {name, symbolsScanned, added, localPath, error?}
    """
    is_url = source.startswith(("http://", "https://", "git@", "git://"))
    local_path: Optional[str] = None
    remote_url = ""
    repo_name = ""

    if is_url:
        remote_url = source
        repo_name = _name_from_url(source)
        local_path = str(_REPOS_DIR / repo_name)
        error = _clone_or_pull(source, local_path)
        if error:
            return {"name": repo_name, "added": 0, "error": error}
    else:
        local_path = os.path.abspath(source)
        if not os.path.isdir(local_path):
            return {"name": source, "added": 0, "error": f"Path not found: {local_path}"}
        repo_name = Path(local_path).name

    symbols = _extract_symbols(local_path)
    if not symbols:
        return {"name": repo_name, "added": 0, "error": "No indexable symbols found"}

    def wrapped_progress(done: int, total: int) -> None:
        if progress_cb:
            progress_cb(done, total)

    active_runtime = runtime or create_runtime(
        runtime_name=choose_runtime_for_stage(stage="knowledge", workload="knowledge extraction"),
        workspace_path=local_path,
    )

    extractor = KnowledgeExtractor(
        store=store,
        runtime=active_runtime,
        source_repo=repo_name,
        source_repo_url=remote_url,
        focus=focus,
        workspace_path=local_path,
        progress_cb=wrapped_progress,
    )

    added = extractor.extract_from_symbols(symbols)
    store.upsert_repo(repo_name, local_path, remote_url)

    log.info("git_importer: '%s' — %d symbols, %d entries added", repo_name, len(symbols), added)
    return {
        "name": repo_name,
        "symbolsScanned": len(symbols),
        "added": added,
        "localPath": local_path,
    }


def _clone_or_pull(url: str, dest: str) -> Optional[str]:
    """Clone if dest doesn't exist, otherwise git pull. Returns error string or None."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)

    if os.path.isdir(os.path.join(dest, ".git")):
        log.info("git_importer: pulling existing clone at %s", dest)
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=dest,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            log.warning("git pull failed: %s", result.stderr)
            # Non-fatal — use existing clone
        return None
    else:
        log.info("git_importer: cloning %s → %s", url, dest)
        result = subprocess.run(
            ["git", "clone", "--depth=1", url, dest],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return f"git clone failed: {result.stderr.strip()}"
        return None


def _extract_symbols(repo_path: str) -> list[dict]:
    """Use the graph indexer to extract AST symbols from the repo."""
    try:
        from backend.graph.indexer import collect_files, parse_file

        repo_name = Path(repo_path).name
        files = collect_files(repo_path)
        symbols: list[dict] = []

        for file_path in files:
            result = parse_file(str(file_path), project=repo_name, root_path=repo_path)
            if result is None:
                continue
            for sym in result.symbols:
                if sym.label not in ("function", "method", "class"):
                    continue
                body = sym.body or ""
                if not body.strip():
                    continue
                symbols.append({
                    "name": sym.name,
                    "label": sym.label,
                    "file_path": sym.file_path,
                    "body": body,
                    "qualified_name": sym.qualified_name,
                })

        return symbols
    except Exception as exc:
        log.warning("_extract_symbols failed: %s", exc)
        return []


def _name_from_url(url: str) -> str:
    """Derive a short repo name from a git URL."""
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "imported-repo"
