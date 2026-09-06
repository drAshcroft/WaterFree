"""Where the asset library lives on this machine.

The tools ship with the skill, but the asset collections and the index they
build are per-machine. Everything resolves through here so a different machine
-- or a second library -- only has to set environment variables, not edit code.

    WATERFREE_ASSETS_HOME    where the catalogs and asset-index.db are written
                             (default C:\\Projects\\itch_assets)
    WATERFREE_UNITY_CACHE    the Unity Asset Store download cache
                             (default %APPDATA%\\Unity\\Asset Store-5.x)

Which directories get scanned is data, not code, so it lives in
`asset-sources.json` inside HOME. If that file is absent the built-in defaults
below are used, which is what this machine already had.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(
    os.environ.get("WATERFREE_ASSETS_HOME", r"C:\Projects\itch_assets")
).expanduser()

UNITY_CACHE = Path(
    os.environ.get(
        "WATERFREE_UNITY_CACHE",
        str(Path.home() / "AppData/Roaming/Unity/Asset Store-5.x"),
    )
).expanduser()

CATALOG = HOME / "asset-catalog.json"
CACHE_CATALOG = HOME / "unity-cache-catalog.json"
MARKDOWN = HOME / "ASSET-CATALOG.md"
DB = HOME / "asset-index.db"
SOURCES = HOME / "asset-sources.json"

# Roots scanned for already-extracted packs, as (path, label) pairs. A root is
# a directory whose immediate subdirectories are packs.
DEFAULT_ROOTS = [
    (r"C:\Projects\kenny_assets", "kenney"),
    (r"C:\Projects\humble\humble\Assets", "unity"),
]


def roots() -> list[tuple[Path, str]]:
    """Scan roots, from asset-sources.json when present."""
    if SOURCES.exists():
        data = json.loads(SOURCES.read_text(encoding="utf-8"))
        return [(Path(r["path"]), r.get("kind", "other")) for r in data["roots"]]
    return [(Path(p), kind) for p, kind in DEFAULT_ROOTS]


def describe() -> str:
    lines = [
        f"library home : {HOME}{'' if HOME.is_dir() else '   (MISSING)'}",
        f"search index : {DB}{'' if DB.exists() else '   (not built)'}",
        f"unity cache  : {UNITY_CACHE}{'' if UNITY_CACHE.is_dir() else '   (not found)'}",
        f"sources file : {SOURCES if SOURCES.exists() else '(using built-in defaults)'}",
        "scan roots   :",
    ]
    for path, kind in roots():
        lines.append(f"   {'ok ' if path.is_dir() else 'MISSING'} [{kind}] {path}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
