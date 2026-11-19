"""Keep stray scripts from shadowing real packages in the backend process.

The backend runs from the shared venv (``C:\\Projects\\.local`` by default). On
Windows, ``site`` puts the venv root itself on ``sys.path`` (it is one of the
prefix "site dirs"), and ``python -m backend.main`` prepends the caller's
working directory. Agents write one-off patch scripts into exactly those
folders, so a scratch ``h2.py`` once replaced the real ``h2`` package and broke
``import transformers`` for imagegen, asset search and the test suite
(WaterFree task IMG-SYSPATH-001).

``harden_sys_path`` drops those two entries at startup. ``find_shadowing``
reports any installed package that currently resolves to a loose ``.py`` file
outside site-packages, so ``status`` commands can name the culprit instead of
surfacing an unrelated import error.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path or os.curdir))


def clean_path_entries(
    entries: list[str],
    *,
    prefix: str,
    base_prefix: str,
    cwd: str,
    backend_parent: str,
) -> tuple[list[str], list[str]]:
    """Split ``entries`` into (kept, removed).

    Removed: the venv root (only when running inside a venv, i.e. ``prefix``
    differs from ``base_prefix``), and the working directory (``''`` or its
    absolute form) unless it is the folder that holds the running ``backend``
    package — the dev-checkout case, where it is how ``backend`` was found.
    """
    venv_root = _norm(prefix) if _norm(prefix) != _norm(base_prefix) else None
    cwd_norm = _norm(cwd)
    keep_cwd = cwd_norm == _norm(backend_parent)
    kept: list[str] = []
    removed: list[str] = []
    for entry in entries:
        norm = _norm(entry) if entry else cwd_norm
        if venv_root is not None and norm == venv_root:
            removed.append(entry)
        elif norm == cwd_norm and not keep_cwd:
            removed.append(entry)
        else:
            kept.append(entry)
    return kept, removed


def harden_sys_path() -> list[str]:
    """Remove the venv root and a foreign cwd from ``sys.path``; return what was removed."""
    backend_parent = str(Path(__file__).resolve().parent.parent)
    kept, removed = clean_path_entries(
        list(sys.path),
        prefix=sys.prefix,
        base_prefix=getattr(sys, "base_prefix", sys.prefix),
        cwd=os.getcwd(),
        backend_parent=backend_parent,
    )
    if removed:
        sys.path[:] = kept
    return removed


def _site_dirs() -> list[str]:
    dirs = [p for p in sys.path if p and os.path.basename(os.path.normpath(p)).lower() == "site-packages"]
    stdlib = os.path.dirname(os.__file__)
    return [_norm(d) for d in dirs] + [_norm(stdlib)]


def find_shadowing(names: list[str] | None = None) -> list[dict]:
    """Installed top-level modules that resolve to a loose file outside site-packages/stdlib.

    ``names`` defaults to every top-level import name of an installed
    distribution. Uses ``find_spec``, which locates a module without executing
    it, so a broken shadow file is reported rather than run.
    """
    try:
        from importlib.metadata import packages_distributions  # noqa: PLC0415
        dist_map = packages_distributions()
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        dist_map = {}
    candidates = names if names is not None else sorted(dist_map)
    allowed = _site_dirs()
    found: list[dict] = []
    for name in candidates:
        if not name.isidentifier() or name in sys.builtin_module_names:
            continue
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError, AttributeError):
            continue
        origin = getattr(spec, "origin", None) if spec else None
        if not origin or origin in ("built-in", "frozen"):
            continue
        origin_norm = _norm(origin)
        if any(origin_norm.startswith(d + os.sep) for d in allowed):
            continue
        # A package directory outside site-packages is normally an editable
        # install (a .pth pointing at a src/ tree); a loose single-file module
        # with an installed package's name is the scratch-script hazard.
        if os.path.basename(origin_norm) == "__init__.py":
            continue
        found.append({
            "module": name,
            "file": origin,
            "shadows": ", ".join(dist_map.get(name, [])) or "an installed package",
        })
    return found
