"""
Repo scanning utilities — reads structure, README, manifests, and source files
without any external dependencies.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_README_NAMES = {"readme.md", "readme.txt", "readme.rst", "readme"}

_MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "requirements.txt", "gemfile", "composer.json",
}

_IGNORE_DIRS = {
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".venv", "venv", "env", ".env",
    "dist", "build", "target", "out", ".next", ".nuxt",
    "vendor", "bower_components", ".tox", ".mypy_cache",
    ".pytest_cache", "coverage", ".cache",
}

_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx",
    ".go", ".rs", ".java", ".kt", ".swift",
    ".c", ".cpp", ".h", ".hpp", ".cs",
    ".rb", ".php", ".scala", ".clj", ".ex", ".exs",
}


# ---------------------------------------------------------------------------
# Repo overview helpers
# ---------------------------------------------------------------------------

def read_readme(repo_path: Path, max_chars: int = 4000) -> str:
    """Return the first README found in the repo root (up to max_chars)."""
    try:
        for entry in sorted(repo_path.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file() and entry.name.lower() in _README_NAMES:
                return entry.read_text(errors="replace")[:max_chars]
    except Exception:
        pass
    return ""


def read_manifest(repo_path: Path, max_chars: int = 2000) -> str:
    """Return the content of the first manifest/config file found (up to max_chars)."""
    try:
        for entry in sorted(repo_path.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file() and entry.name.lower() in _MANIFEST_NAMES:
                return entry.read_text(errors="replace")[:max_chars]
    except Exception:
        pass
    return ""


def file_tree(repo_path: Path, max_depth: int = 3, max_entries: int = 150) -> str:
    """
    Build a text representation of the repo directory tree.
    Skips hidden directories (except .github) and noise folders.
    """
    lines: list[str] = [f"{repo_path.name}/"]

    def _walk(path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth or len(lines) >= max_entries:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return

        for entry in entries:
            if len(lines) >= max_entries:
                lines.append(f"{prefix}├── ...")
                return
            name = entry.name
            if name.startswith(".") and name not in {".github", ".env.example"}:
                continue
            if entry.is_dir() and name in _IGNORE_DIRS:
                continue
            lines.append(f"{prefix}├── {name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir():
                _walk(entry, prefix + "│   ", depth + 1)

    _walk(repo_path, "", 1)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Source file collection
# ---------------------------------------------------------------------------

def collect_source_files(
    repo_path: Path,
    extensions: set[str] | None = None,
    max_files: int = 500,
) -> list[Path]:
    """Walk the repo and return paths to all source files."""
    exts = extensions if extensions is not None else _CODE_EXTENSIONS
    result: list[Path] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [
            d for d in sorted(dirs)
            if d not in _IGNORE_DIRS and not d.startswith(".")
        ]
        for fname in sorted(files):
            path = Path(root) / fname
            if path.suffix.lower() in exts:
                result.append(path)
            if len(result) >= max_files:
                return result
    return result


def read_file_safe(path: Path, max_chars: int = 8000) -> str:
    """Read a source file, returning up to max_chars characters."""
    try:
        return path.read_text(errors="replace")[:max_chars]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_git_remote(repo_path: Path) -> str:
    """Return the git remote origin URL, or empty string if unavailable."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def find_files_matching_hints(
    repo_path: Path,
    hints: list[str],
    source_files: list[Path],
    max_matches: int = 5,
) -> list[Path]:
    """
    Given a list of path hints (e.g. ["src/auth", "lib/token"]), return source
    files whose repo-relative path contains any of those hints.
    Falls back to the first few source files if nothing matches.
    """
    matched: list[Path] = []
    for hint in hints:
        hint_norm = hint.replace("\\", "/").lower().strip("/")
        for src in source_files:
            rel = str(src.relative_to(repo_path)).replace("\\", "/").lower()
            if hint_norm in rel or rel.startswith(hint_norm):
                if src not in matched:
                    matched.append(src)
            if len(matched) >= max_matches:
                return matched
    return matched or source_files[:3]
