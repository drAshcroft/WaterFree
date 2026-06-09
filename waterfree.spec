# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — builds a one-dir waterfree runtime.

Build:
    pyinstaller waterfree.spec
Output:
    dist/waterfree-win32-x64/waterfree.exe   (Windows)
    dist/waterfree-darwin-arm64/waterfree    (macOS arm64)
    dist/waterfree-linux-x64/waterfree       (Linux)
"""

import importlib.util
import platform
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

_ROOT = Path(globals().get("SPECPATH", Path.cwd())).resolve()


def _require_build_deps(modules: list[str]) -> None:
    """
    Abort the build if a required runtime dependency is not importable in the
    build environment.

    PyInstaller silently omits hidden imports / collect_all targets that aren't
    installed, producing an exe that imports fine but degrades at runtime
    (indexer -> regex fallback, graphify -> 'No module named networkx'). Failing
    here guarantees every declared dependency actually makes it into the bundle.
    Keep this list aligned with backend/diagnostics.py and backend/requirements.txt.
    """
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        raise SystemExit(
            "\nwaterfree.spec: build environment is missing required packages:\n"
            + "\n".join(f"  - {m}" for m in missing)
            + "\n\nInstall them before building:\n"
            "  pip install -r backend/requirements.txt\n"
            "(Building without them would ship a degraded runtime.)\n"
        )

# ---------------------------------------------------------------------------
# Platform-specific output name
# ---------------------------------------------------------------------------
_os   = sys.platform          # win32 | darwin | linux
_arch = platform.machine().lower().replace("amd64", "x64").replace("x86_64", "x64")
runtime_name = f"waterfree-{_os}-{_arch}"
launcher_name = "waterfree"  # PyInstaller adds .exe on Windows.

# ---------------------------------------------------------------------------
# Collect tree-sitter language packages (they include compiled .so/.pyd files
# and internal data that PyInstaller won't find through static analysis)
# ---------------------------------------------------------------------------
_ts_pkgs = [
    # Core runtime
    "tree_sitter",
    # Original 5 languages
    "tree_sitter_python",
    "tree_sitter_typescript",
    "tree_sitter_javascript",
    "tree_sitter_c_sharp",
    # graphify 40-language expansion
    "tree_sitter_go",
    "tree_sitter_rust",
    "tree_sitter_java",
    "tree_sitter_groovy",
    "tree_sitter_c",
    "tree_sitter_cpp",
    "tree_sitter_ruby",
    "tree_sitter_kotlin",
    "tree_sitter_scala",
    "tree_sitter_php",
    "tree_sitter_swift",
    "tree_sitter_lua",
    "tree_sitter_zig",
    "tree_sitter_powershell",
    "tree_sitter_elixir",
    "tree_sitter_objc",
    "tree_sitter_julia",
    "tree_sitter_bash",
    "tree_sitter_json",
]

# Non-grammar runtime packages the bundle cannot function without. Mirrors
# backend/requirements.txt and backend/diagnostics.py _REQUIRED_IMPORTS.
_required_runtime_pkgs = [
    "networkx",
    "datasketch",
    "rapidfuzz",
    "pathspec",
    "pydantic",
    "anthropic",
    "langchain_core",
    "langchain_anthropic",
    "langchain_openai",
    "langchain_ollama",
    "deepagents",
]

# Fail the build now if anything required is absent, rather than silently
# shipping a degraded exe.
_require_build_deps(_ts_pkgs + _required_runtime_pkgs)

datas_extra    = []
binaries_extra = []
hiddenimports_extra = []

for pkg in _ts_pkgs:
    # Required (checked above), so a collect failure here is a real error.
    d, b, h = collect_all(pkg)
    datas_extra    += d
    binaries_extra += b
    hiddenimports_extra += h

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

# Collect graphify package — includes skill*.md and always_on/*.md data files
try:
    d, b, h = collect_all("backend.graphify")
    datas_extra    += d
    binaries_extra += b
    hiddenimports_extra += h
except Exception:
    pass

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
    # graphify graph engine
    "networkx",
    "networkx.algorithms",
    "networkx.classes",
    "datasketch",
    "rapidfuzz",
    "unicodedata",
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
        # Keep the binary small — none of these are used at runtime
        "tkinter",
        "matplotlib",      # graphify svg export — not needed in WaterFree
        "PIL",
        "cv2",
        "pytest",
        "IPython",
        "notebook",
        # numpy/scipy ARE needed by datasketch (graphify dep) — do not exclude
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=launcher_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # UPX can corrupt native extensions; leave off by default
    console=True,    # `waterfree serve` uses stdio
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=runtime_name,
    strip=False,
    upx=False,       # UPX can corrupt native extensions; leave off by default
)
