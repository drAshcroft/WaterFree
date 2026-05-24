# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — builds a single self-contained waterfree executable.

Build:
    pyinstaller waterfree.spec
Output:
    bin/waterfree-win32-x64.exe   (Windows)
    bin/waterfree-darwin-arm64    (macOS arm64)
    bin/waterfree-linux-x64       (Linux)
"""

import platform
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

_ROOT = Path(globals().get("SPECPATH", Path.cwd())).resolve()

# ---------------------------------------------------------------------------
# Platform-specific output name
# ---------------------------------------------------------------------------
_os   = sys.platform          # win32 | darwin | linux
_arch = platform.machine().lower().replace("amd64", "x64").replace("x86_64", "x64")
exe_name = f"waterfree-{_os}-{_arch}"

# ---------------------------------------------------------------------------
# Collect tree-sitter language packages (they include compiled .so/.pyd files
# and internal data that PyInstaller won't find through static analysis)
# ---------------------------------------------------------------------------
_ts_pkgs = [
    "tree_sitter",
    "tree_sitter_python",
    "tree_sitter_typescript",
    "tree_sitter_javascript",
    "tree_sitter_c_sharp",
]

datas_extra    = []
binaries_extra = []
hiddenimports_extra = []

for pkg in _ts_pkgs:
    try:
        d, b, h = collect_all(pkg)
        datas_extra    += d
        binaries_extra += b
        hiddenimports_extra += h
    except Exception:
        pass  # package not installed — skip gracefully

def _collect_data_tree(source: Path, dest: str) -> list[tuple[str, str]]:
    if not source.exists():
        return []
    entries: list[tuple[str, str]] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel_parent = path.relative_to(source).parent
        entries.append((str(path), str(Path(dest) / rel_parent)))
    return entries


def _collect_backend_hiddenimports() -> list[str]:
    root = _ROOT / "backend"
    if not root.exists():
        return []
    modules: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(_ROOT).with_suffix("")
        parts = list(rel.parts)
        if "tests" in parts or "__pycache__" in parts:
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules.append(".".join(parts))
    return sorted(set(modules))


datas_extra += _collect_data_tree(
    _ROOT / "backend" / "llm" / "personas" / "initial_personas",
    "backend/llm/personas/initial_personas",
)

runtime_hiddenimports = [
    # Backend modules are imported through argv dispatch, registries, and handlers.
    *_collect_backend_hiddenimports(),
    # Common provider and runtime modules.
    "anthropic",
    "langchain_anthropic",
    "langchain_openai",
    "langchain_ollama",
    "langchain_google_genai",
    "langchain_groq",
    "langchain_core",
    "langchain_core.messages",
    "langchain_core.tools",
    "deepagents",
    "deepagents.backends",
    "deepagents.middleware",
    "huggingface_hub",
    "pydantic",
    # stdlib extras that can get missed by frozen optional code paths.
    "sqlite3",
    "json",
    "pathlib",
    # gitignore pattern matching for indexer.
    "pathspec",
    "pathspec.patterns",
    "pathspec.patterns.gitwildmatch",
]

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    [str(_ROOT / "backend" / "main.py")],
    pathex=[str(_ROOT)],
    binaries=binaries_extra,
    datas=datas_extra,
    hiddenimports=sorted(set(hiddenimports_extra + runtime_hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the binary small — none of these are used
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "PIL",
        "cv2",
        "pytest",
        "IPython",
        "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # UPX can corrupt native extensions; leave off by default
    console=True,    # MCP servers need stdio
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
