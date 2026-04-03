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
from PyInstaller.utils.hooks import collect_all

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

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    ["backend/main.py"],
    pathex=["."],
    binaries=binaries_extra,
    datas=datas_extra,
    hiddenimports=hiddenimports_extra + [
        # All MCP server modules (imported dynamically based on argv)
        "backend.mcp_index",
        "backend.mcp_knowledge",
        "backend.mcp_todos",
        "backend.mcp_debug",
        "backend.mcp_testing",
        "backend.mcp_qa_summary",
        # VS Code bridge server
        "backend.server",
        # Common provider and runtime modules
        "anthropic",
        "mcp",
        "mcp.server.stdio",
        "langchain_anthropic",
        "langchain_openai",
        "langchain_ollama",
        "langchain_core",
        "langchain_core.messages",
        "langchain_core.tools",
        "deepagents",
        # stdlib extras that get missed
        "sqlite3",
        "json",
        "pathlib",
    ],
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
