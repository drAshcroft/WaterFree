"""Index the Unity Asset Store download cache.

These are packages bought/claimed on the Asset Store and downloaded, but not
necessarily imported into any project -- 23GB of .unitypackage archives that
are otherwise invisible. Without an index the only way to know what is in one
is to import it and look.

A .unitypackage is a gzipped tar where every asset is a directory named after
its GUID containing `pathname` (the original Assets/... path), `asset` (the
bytes) and `asset.meta`. Reading just the `pathname` members gives a complete
manifest without extracting anything, at roughly 150 MB/s.

Writes unity-cache-catalog.json next to this script. build_asset_catalog.py
picks that file up if it exists, so the cached packages appear in the main
catalog alongside the imported ones.

Re-run only when new packages are downloaded -- it reads all 23GB.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from datetime import date, datetime
from pathlib import Path

import assetpaths

CACHE = assetpaths.UNITY_CACHE
OUT = assetpaths.CACHE_CATALOG

# A package nested three deep would be pathological; stop looking there.
MAX_DEPTH = 2

MODEL_EXT = {".glb", ".gltf", ".fbx", ".obj", ".dae", ".stl", ".blend"}
TEXTURE_EXT = {".png", ".jpg", ".jpeg", ".psd", ".tga", ".exr", ".tif", ".tiff"}
AUDIO_EXT = {".wav", ".ogg", ".mp3", ".flac", ".aiff"}
FONT_EXT = {".ttf", ".otf"}
# Worth counting, but never the thing you search a pack for.
CODE_EXT = {".cs", ".shader", ".shadergraph", ".compute", ".hlsl", ".cginc"}
UNITY_EXT = {".prefab", ".mat", ".anim", ".controller", ".unity", ".asset"}


def classify(ext: str) -> str | None:
    if ext in MODEL_EXT:
        return "model"
    if ext in FONT_EXT:
        return "font"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in TEXTURE_EXT:
        return "texture"
    if ext in CODE_EXT:
        return "code"
    if ext in UNITY_EXT:
        return "unity"
    return None


def pick_primary(by_kind: dict[str, list[str]]) -> str:
    """Choose the kind a pack should be indexed and searched by.

    Models win outright whenever present: a 3D pack always ships more textures
    than meshes (albedo/normal/roughness per model), so counting would index
    the wrong thing. Nothing else has that parasitic relationship -- fonts and
    audio do not tag along with textures -- so among the rest, most files wins.
    That keeps a GUI pack with a few bundled fonts indexed as images.
    """
    if by_kind.get("model"):
        return "model"
    rest = {k: v for k, v in by_kind.items() if k in ("font", "audio", "texture")}
    if not rest:
        return "none"
    return max(rest, key=lambda k: len(rest[k]))


def _walk(fileobj, prefix: str, depth: int) -> list[str]:
    """Read one package's manifest, recursing into nested packages.

    Several publishers (SICS, Tidal Flask) ship an outer package that contains
    nothing but a readme and two inner .unitypackage files, one per render
    pipeline. Indexing only the outer layer reports those packs as empty, which
    is exactly backwards -- they are some of the largest here.

    Streaming ('r|gz') cannot seek, so a nested package has to be read into
    memory before it can be opened. They are a few hundred MB at worst, and the
    alternative is extracting 23GB to disk.
    """
    paths: list[str] = []
    # GUID dir -> path, for entries that are themselves .unitypackage files.
    inner: dict[str, str] = {}

    with tarfile.open(fileobj=fileobj, mode="r|gz") as tf:
        for member in tf:
            if not member.name.endswith("/pathname"):
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            text = fh.read().decode("utf-8", "replace").splitlines()
            if not text:
                continue
            path = text[0]
            paths.append(prefix + path)
            if path.lower().endswith(".unitypackage") and depth < MAX_DEPTH:
                inner[member.name.rsplit("/", 1)[0]] = path

    if not inner:
        return paths

    # Second pass. Within a GUID directory the tar stores `asset` BEFORE
    # `pathname`, so a single streaming pass has already gone past the bytes by
    # the time it learns the entry is a nested package. Rewinding and re-reading
    # costs one extra decompress, but only for the handful of packages that
    # actually nest -- and those are exactly the ones that look empty otherwise.
    fileobj.seek(0)
    with tarfile.open(fileobj=fileobj, mode="r|gz") as tf:
        for member in tf:
            if not member.name.endswith("/asset"):
                continue
            guid = member.name.rsplit("/", 1)[0]
            if guid not in inner:
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            label = f"{prefix}[{Path(inner[guid]).name}] "
            try:
                paths += _walk(io.BytesIO(fh.read()), label, depth + 1)
            except Exception as exc:
                paths.append(f"{label}<<unreadable: {type(exc).__name__}>>")
    return paths


def read_manifest(pkg: Path) -> list[str]:
    """Return every original asset path inside the package."""
    with open(pkg, "rb") as fh:
        return _walk(fh, "", 0)


def index_package(pkg: Path) -> dict:
    rel = pkg.relative_to(CACHE)
    parts = rel.parts
    publisher = parts[0]
    # Unity flattens the store's category tree into one directory name, e.g.
    # "3D ModelsEnvironmentsFantasy". Leave it whole -- splitting it correctly
    # would need the store's category list, and it is still searchable as-is.
    category = parts[1] if len(parts) > 2 else ""

    record = {
        "package": pkg.stem,
        "publisher": publisher,
        "store_category": category,
        "file": str(pkg),
        "size_mb": round(pkg.stat().st_size / 1048576, 1),
        "downloaded": datetime.fromtimestamp(pkg.stat().st_mtime).date().isoformat(),
    }

    try:
        paths = read_manifest(pkg)
    except Exception as exc:  # a truncated or half-downloaded archive
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["counts"] = {}
        record["distinct_objects"] = []
        record["distinct_object_count"] = 0
        record["primary_kind"] = "unreadable"
        return record

    counts: dict[str, int] = {}
    by_kind: dict[str, list[str]] = {}
    for p in paths:
        ext = Path(p).suffix.lower()
        kind = classify(ext)
        if kind is None:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ("model", "font", "audio", "texture"):
            by_kind.setdefault(kind, []).append(p)

    primary = pick_primary(by_kind)
    stems = sorted({Path(p).stem for p in by_kind.get(primary, [])})

    record["asset_count"] = len(paths)
    record["counts"] = counts
    record["primary_kind"] = primary
    record["distinct_objects"] = stems
    record["distinct_object_count"] = len(stems)
    # The full path list is what makes "which package has a windmill" answerable.
    record["asset_paths"] = sorted(paths)
    return record


def main() -> None:
    if not CACHE.is_dir():
        raise SystemExit(f"cache not found: {CACHE}")

    packages = sorted(CACHE.rglob("*.unitypackage"))
    print(f"indexing {len(packages)} packages from {CACHE}\n")

    out = []
    started = time.time()
    for i, pkg in enumerate(packages, 1):
        t0 = time.time()
        rec = index_package(pkg)
        flag = " !! " + rec["error"][:60] if "error" in rec else ""
        print(
            f"  [{i:3}/{len(packages)}] {rec['publisher'][:22]:22} "
            f"{rec['package'][:40]:40} {rec['size_mb']:7.1f}MB "
            f"{rec['distinct_object_count']:5} {rec['primary_kind']:8} "
            f"{time.time() - t0:5.1f}s{flag}",
            flush=True,
        )
        out.append(rec)

    catalog = {
        "generated": date.today().isoformat(),
        "generator": "build_unity_cache_index.py",
        "cache_path": str(CACHE),
        "note": (
            "Downloaded Asset Store packages. Presence here means the package is "
            "on disk and licensed to this account -- NOT that it is imported into "
            "any project. All are governed by the Unity Asset Store EULA unless a "
            "licence file inside the package says otherwise."
        ),
        "package_count": len(out),
        "total_size_mb": round(sum(r["size_mb"] for r in out), 1),
        "packages": out,
    }
    OUT.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

    ok = [r for r in out if "error" not in r]
    print(f"\nindexed {len(ok)}/{len(out)} packages in {time.time() - started:.0f}s")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1048576:.1f} MB)")
    for r in out:
        if "error" in r:
            print(f"  UNREADABLE: {r['publisher']}/{r['package']} -- {r['error']}")


if __name__ == "__main__":
    main()
