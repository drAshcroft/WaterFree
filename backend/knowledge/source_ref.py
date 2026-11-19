"""Where an entry's `source_file` stood in git when it was filed.

Why this exists: agents working in parallel worktrees file lessons that cite a
file (or a class inside it) that exists only in their own uncommitted
worktree. A second agent on `main` then assumes the file is there, or creates
a clashing file / `class_name`. Recording the branch, the worktree and whether
the file was committed lets search rows say "uncommitted in worktree X".

Cost model: one `git status` call per add/update that names a source file;
reading the record back costs nothing, and the reader side (`ReaderContext`)
only walks the filesystem for the `.git` marker, never runs git.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_GIT_TIMEOUT_SECONDS = 5.0
# Variables a git hook (or a parent git process) may export; they would
# override `-C <worktree>` and point the status call at the wrong repository.
_GIT_ENV_OVERRIDES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")


def find_worktree_root(start: Path) -> Path | None:
    """The nearest ancestor (or `start` itself) holding a `.git` dir or file."""
    current = start
    while not current.exists() and current != current.parent:
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def repo_name(worktree_root: Path) -> str:
    """The main checkout's directory name, also for a linked worktree.

    A linked worktree's `.git` is a file (`gitdir: <main>/.git/worktrees/<name>`);
    its `commondir` file, or failing that the `worktrees` parent, leads back to
    the main repository, whose directory name is what agents pass as
    `--source-repo`.
    """
    marker = worktree_root / ".git"
    if not marker.is_file():
        return worktree_root.name
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return worktree_root.name
    if not text.startswith("gitdir:"):
        return worktree_root.name
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = (worktree_root / gitdir).resolve()
    common: Path | None = None
    try:
        rel = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        common = (gitdir / rel).resolve()
    except OSError:
        if gitdir.parent.name == "worktrees":
            common = gitdir.parent.parent
    if common is None:
        return worktree_root.name
    return common.parent.name if common.name == ".git" else common.name


def capture_source_ref(source_file: str, base_dir: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Branch / worktree / commit status of `source_file`, or None outside git.

    `source_file` is resolved against `base_dir` (the --workspace or cwd) when
    relative. Never raises: a missing git binary, a timeout or a broken
    repository all return None, because filing the lesson matters more than
    annotating it.
    """
    raw = (source_file or "").strip()
    if not raw:
        return None
    try:
        path = Path(raw)
        if not path.is_absolute():
            path = Path(base_dir) / path
        path = Path(os.path.abspath(path))
        root = find_worktree_root(path.parent)
        if root is None:
            return None
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            return None
        if not rel or rel == ".":
            return None
        status = _git_status(root, rel)
        if status is None:
            return None
        branch, commit, file_status = status
        if file_status is None:
            file_status = "clean" if path.exists() else "missing"
        return {
            "repo": repo_name(root),
            "worktreePath": root.as_posix(),
            "branch": branch,
            "commit": commit,
            "path": rel,
            "status": file_status,
            "committed": file_status == "clean",
        }
    except Exception:  # pragma: no cover - annotating must never block an add
        return None


def _git_status(root: Path, rel: str) -> tuple[str, str, str | None] | None:
    """(branch, commit, file status or None when git printed nothing for it)."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES}
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v2", "--branch",
             "--untracked-files=all", "--ignored=matching", "-z", "--", rel],
            capture_output=True, timeout=_GIT_TIMEOUT_SECONDS, env=env, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    branch, commit = "", ""
    file_status: str | None = None
    records = proc.stdout.decode("utf-8", errors="replace").split("\0")
    skip_next = False
    for record in records:
        if skip_next:  # the original path that follows a rename record
            skip_next = False
            continue
        if record.startswith("# branch.oid "):
            value = record[len("# branch.oid "):].strip()
            commit = "" if value == "(initial)" else value
        elif record.startswith("# branch.head "):
            branch = record[len("# branch.head "):].strip()
        elif record.startswith("? "):
            file_status = "untracked"
        elif record.startswith("! "):
            file_status = "ignored"
        elif record.startswith("u "):
            file_status = "unmerged"
        elif record.startswith("2 "):
            file_status = "renamed"
            skip_next = True
        elif record.startswith("1 "):
            xy = record[2:4]
            if "A" in xy[:1]:
                file_status = "added"
            elif "D" in xy:
                file_status = "deleted"
            else:
                file_status = "modified"
    return branch, commit, file_status


@dataclass
class ReaderContext:
    """Who is reading: their checkout root and the names their repo goes by."""

    root: Path | None = None
    repo_keys: set[str] = field(default_factory=set)

    @classmethod
    def for_workspace(cls, workspace: str | os.PathLike[str] | None) -> "ReaderContext":
        try:
            base = Path(os.path.abspath(workspace or os.getcwd()))
        except OSError:  # pragma: no cover - cwd deleted under us
            return cls()
        keys = {base.name.casefold()} if base.name else set()
        root = find_worktree_root(base)
        if root is not None:
            keys.add(root.name.casefold())
            keys.add(repo_name(root).casefold())
        keys.discard("")
        return cls(root=root or base, repo_keys=keys)

    def note_for(self, *, source_ref: dict[str, Any] | None, source_file: str, source_repo: str) -> str | None:
        """A one-line caveat for a row, or None when there is nothing to say."""
        parts: list[str] = []
        if source_ref and source_ref.get("committed") is False:
            status = source_ref.get("status") or "uncommitted"
            where = source_ref.get("worktreePath") or "?"
            branch = source_ref.get("branch") or "?"
            if status == "missing":
                parts.append(f"source file was missing from worktree {where} (branch {branch}) when filed")
            else:
                parts.append(f"uncommitted in worktree {where} (branch {branch}, {status} when filed)")
        rel = (source_ref or {}).get("path") or source_file
        if rel and self.root is not None and self._same_repo(source_ref, source_repo):
            candidate = Path(rel)
            if not candidate.is_absolute() and not (self.root / candidate).exists():
                parts.append(f"{candidate.as_posix()} is not in this workspace")
        return "; ".join(parts) or None

    def _same_repo(self, source_ref: dict[str, Any] | None, source_repo: str) -> bool:
        if not self.repo_keys:
            return False
        names = {_basename_key(source_repo)}
        if source_ref:
            names.add(str(source_ref.get("repo") or "").casefold())
        names.discard("")
        return bool(names & self.repo_keys)


def _basename_key(repo: str) -> str:
    text = (repo or "").strip().replace("\\", "/").rstrip("/")
    return text.rsplit("/", 1)[-1].casefold() if text else ""
