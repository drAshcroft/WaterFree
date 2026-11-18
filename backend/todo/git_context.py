"""
Git facts the todos CLI needs, and nothing else.

Two jobs:

* **Store resolution from linked worktrees.** Agents doing parallel work run in
  `git worktree` checkouts (e.g. `.claude/worktrees/agent-*`). The backlog lives
  in the *main* checkout's `.waterfree/tasks.db`, so a worktree with no store of
  its own is mapped there (`resolve_todos_workspace`). Only the todos area does
  this — testing/index must keep operating on the worktree's own files.
* **Work references.** Recording which branch/checkout a task's work happened in
  (`detect_work_ref`) and reporting that checkout's live state
  (`live_work_status`), so finished work on an unmerged branch stays findable.

Every git call is best-effort: a missing git binary, a non-repo directory or a
deleted worktree yields `None`/partial data, never an exception.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_STORE_RELATIVE = Path(".waterfree") / "tasks.db"
_GIT_TIMEOUT_SECONDS = 10
# Environment variables that would make `git -C <path>` ignore <path>.
_GIT_ENV_OVERRIDES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")
_BASE_BRANCH_CANDIDATES = ("main", "master")


def store_path(workspace: str | os.PathLike[str]) -> Path:
    return Path(workspace) / _STORE_RELATIVE


def has_store(workspace: str | os.PathLike[str]) -> bool:
    return store_path(workspace).is_file()


def display_path(path: str | os.PathLike[str]) -> str:
    """Forward-slash form for JSON output (no doubled backslashes on Windows)."""
    return Path(path).as_posix()


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _git(cwd: str | os.PathLike[str], *args: str) -> Optional[str]:
    """Run `git -C cwd args...`; stdout on success, None on any failure."""
    env = {key: value for key, value in os.environ.items() if key not in _GIT_ENV_OVERRIDES}
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


# ── Checkout identity ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CheckoutInfo:
    toplevel: Path
    git_dir: Path
    common_dir: Path

    @property
    def is_linked_worktree(self) -> bool:
        return not same_path(self.git_dir, self.common_dir)

    @property
    def main_checkout(self) -> Optional[Path]:
        """The primary working tree, when the repository has one (not bare)."""
        if self.common_dir.name != ".git":
            return None
        return self.common_dir.parent

    def same_repository(self, other: "CheckoutInfo") -> bool:
        return same_path(self.common_dir, other.common_dir)


def checkout_info(path: str | os.PathLike[str]) -> Optional[CheckoutInfo]:
    """Identify the git checkout containing `path`, or None outside one."""
    out = _git(path, "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir")
    if out is None:
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if len(lines) != 3:
        return None
    base = Path(path)
    # `--git-dir`/`--git-common-dir` may be printed relative to the -C directory.
    toplevel, git_dir, common_dir = (
        Path(item) if Path(item).is_absolute() else (base / item) for item in lines
    )
    return CheckoutInfo(
        toplevel=Path(os.path.realpath(toplevel)),
        git_dir=Path(os.path.realpath(git_dir)),
        common_dir=Path(os.path.realpath(common_dir)),
    )


def looks_like_linked_worktree(path: str | os.PathLike[str]) -> bool:
    """Cheap pre-check (no subprocess): is `path` inside a linked worktree?

    A linked worktree's root holds a `.git` *file* pointing into
    `<common>/.git/worktrees/<name>`. Submodules also use a `.git` file, but
    point into `.git/modules/`, so they are not mistaken for worktrees.
    """
    current = Path(os.path.realpath(path))
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return False
        if marker.is_file():
            try:
                text = marker.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            return "/worktrees/" in text.replace("\\", "/")
    return False


# ── Store resolution ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TodosWorkspace:
    """Where the todos area should open its store, and how it got there."""

    path: str                      # directory whose .waterfree/tasks.db is used
    requested: str                 # what --workspace (or the CWD) resolved to
    resolved_from: Optional[str] = None   # "worktree" when redirected to the main checkout
    worktree: Optional[str] = None        # the linked worktree's root, when redirected

    @property
    def redirected(self) -> bool:
        return self.resolved_from is not None

    def store_exists(self) -> bool:
        return has_store(self.path)


def resolve_todos_workspace(requested: str) -> TodosWorkspace:
    """Map a linked worktree with no task store onto its main checkout.

    * A directory that already has `.waterfree/tasks.db` always keeps it — so a
      worktree that deliberately has its own store keeps using it.
    * Inside a linked worktree whose root has its own store, that store is used.
    * Otherwise, inside a linked worktree, the main checkout's store is used
      (even when it does not exist yet, so a first `add` lands where the rest of
      the project will look for it).
    * Anywhere else the requested directory is used unchanged.
    """
    requested = os.path.abspath(requested)
    if has_store(requested):
        return TodosWorkspace(path=requested, requested=requested)
    if not looks_like_linked_worktree(requested):
        return TodosWorkspace(path=requested, requested=requested)
    info = checkout_info(requested)
    if info is None or not info.is_linked_worktree:
        return TodosWorkspace(path=requested, requested=requested)
    if has_store(info.toplevel):
        return TodosWorkspace(path=str(info.toplevel), requested=requested)
    main = info.main_checkout
    if main is None or not main.is_dir() or same_path(main, info.toplevel):
        return TodosWorkspace(path=requested, requested=requested)
    return TodosWorkspace(
        path=str(main),
        requested=requested,
        resolved_from="worktree",
        worktree=str(info.toplevel),
    )


# ── Work references ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _branch_and_head(path: str | os.PathLike[str]) -> tuple[Optional[str], Optional[str]]:
    """(branch name or None when detached/unborn, HEAD sha or None when unborn)."""
    head = _git(path, "rev-parse", "--verify", "--quiet", "HEAD")
    branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    return (
        (branch or "").strip() or None,
        (head or "").strip() or None,
    )


def work_ref_for_checkout(path: str | os.PathLike[str]) -> Optional[dict]:
    """A `workRef` payload describing the checkout at `path`, or None."""
    info = checkout_info(path)
    if info is None:
        return None
    branch, head = _branch_and_head(info.toplevel)
    return {
        "branch": branch,
        "worktreePath": display_path(info.toplevel),
        "headCommit": head,
        "recordedAt": _now(),
    }


def detect_work_ref(invocation_dir: str, workspace: str) -> Optional[dict]:
    """The `workRef` for work happening in `invocation_dir` on `workspace`'s tasks.

    Only recorded when the invocation directory is a checkout of the *same*
    repository as the task store's workspace; running `todos update` from some
    unrelated directory must not attach that directory's branch to a task.
    """
    here = checkout_info(invocation_dir)
    if here is None:
        return None
    there = checkout_info(workspace)
    if there is None or not here.same_repository(there):
        return None
    branch, head = _branch_and_head(here.toplevel)
    return {
        "branch": branch,
        "worktreePath": display_path(here.toplevel),
        "headCommit": head,
        "recordedAt": _now(),
    }


def explicit_work_ref(*, branch: Optional[str], worktree: Optional[str], workspace: str) -> dict:
    """`workRef` from `--branch` / `--worktree` flags; git fills in what it can."""
    ref: dict = {"branch": None, "worktreePath": None, "headCommit": None, "recordedAt": _now()}
    if worktree:
        resolved = os.path.abspath(worktree)
        info = checkout_info(resolved) if Path(resolved).is_dir() else None
        if info is not None:
            found_branch, head = _branch_and_head(info.toplevel)
            ref.update(worktreePath=display_path(info.toplevel), branch=found_branch, headCommit=head)
        else:
            ref["worktreePath"] = display_path(resolved)
    if branch:
        ref["branch"] = branch
        if not worktree:
            head = _git(workspace, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
            ref["headCommit"] = (head or "").strip() or None
    return ref


def _base_branch(repo: str | os.PathLike[str]) -> Optional[str]:
    for name in _BASE_BRANCH_CANDIDATES:
        if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}") is not None:
            return name
    return None


def _ahead_behind(repo: str | os.PathLike[str], base: str, ref: str) -> Optional[tuple[int, int]]:
    out = _git(repo, "rev-list", "--left-right", "--count", f"{base}...{ref}")
    if out is None:
        return None
    parts = out.split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return ahead, behind


def _newest_receipt(roots: list[Path], task_ref: str) -> Optional[dict]:
    """Newest file under `<root>/artifacts/<task_ref>/` across the given roots."""
    newest: Optional[tuple[float, Path, Path]] = None
    for root in roots:
        folder = root / "artifacts" / task_ref
        if not folder.is_dir():
            continue
        try:
            for item in folder.rglob("*"):
                if not item.is_file():
                    continue
                mtime = item.stat().st_mtime
                if newest is None or mtime > newest[0]:
                    newest = (mtime, item, root)
        except OSError:
            continue
    if newest is None:
        return None
    mtime, item, root = newest
    try:
        shown = item.relative_to(root).as_posix()
    except ValueError:
        shown = item.as_posix()
    return {
        "path": shown,
        "root": display_path(root),
        "modifiedAt": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
    }


def live_work_status(work_ref: dict, *, task_ref: str, workspace: str) -> dict:
    """Current git state for a stored `workRef`. Never raises.

    When the recorded worktree still exists, report its live branch, HEAD,
    dirty-file count and ahead/behind against `main` (or `master`). When it is
    gone, say so and fall back to the recorded branch in the workspace's
    repository, if that branch still exists.
    """
    status: dict = {}
    recorded_path = work_ref.get("worktreePath") or ""
    recorded_branch = work_ref.get("branch") or ""
    path_exists = bool(recorded_path) and Path(recorded_path).is_dir()
    if recorded_path:
        status["pathExists"] = path_exists

    repo: Optional[str] = None
    ref: Optional[str] = None
    if path_exists and checkout_info(recorded_path) is not None:
        repo = recorded_path
        branch, head = _branch_and_head(recorded_path)
        status["branch"] = branch
        status["headCommit"] = head
        dirty = _git(recorded_path, "status", "--porcelain")
        if dirty is not None:
            status["dirtyFiles"] = len([line for line in dirty.splitlines() if line.strip()])
        ref = "HEAD" if head else None
        current_branch = branch
    elif recorded_branch and checkout_info(workspace) is not None:
        exists = _git(workspace, "rev-parse", "--verify", "--quiet", f"refs/heads/{recorded_branch}")
        status["branchExists"] = exists is not None
        if exists is not None:
            repo = workspace
            ref = f"refs/heads/{recorded_branch}"
            status["headCommit"] = exists.strip() or None
        current_branch = recorded_branch
    else:
        current_branch = None

    if repo and ref:
        base = _base_branch(repo)
        if base and base != current_branch:
            counts = _ahead_behind(repo, base, ref)
            if counts is not None:
                status["base"] = base
                status["ahead"], status["behind"] = counts
        elif base:
            status["base"] = base
            status["ahead"], status["behind"] = 0, 0

    roots: list[Path] = []
    if path_exists:
        roots.append(Path(recorded_path))
    if not any(same_path(root, workspace) for root in roots):
        roots.append(Path(workspace))
    receipt = _newest_receipt(roots, task_ref) if task_ref else None
    if receipt:
        status["receipt"] = receipt
    return status
