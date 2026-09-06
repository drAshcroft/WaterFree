"""Build the searchable asset database.

Turns the two catalogs into one SQLite file with a row per individual asset --
not per pack. Every row carries the facets you would actually filter on: what
kind of thing it is, which engines can use it, and what the licence lets you do
with it. Full-text search runs over the asset name, its folder path, its pack,
publisher and theme, so "sci fi gun" finds a mesh called `SciFi_Rifle_01` in a
pack called "Sci-Fi Weapons".

Output: asset-index.db (queried by assetsearch.py).

Run after build_asset_catalog.py.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from asset_context import describe, subject_of
from asset_labels import label_path, tokens

import assetpaths

CATALOG = assetpaths.CATALOG
DB = assetpaths.DB

MODEL_EXT = {".glb", ".gltf", ".fbx", ".obj", ".dae", ".stl", ".blend"}
TEXTURE_EXT = {".png", ".jpg", ".jpeg", ".psd", ".tga", ".exr", ".tif", ".tiff",
               ".svg", ".ai", ".eps"}
AUDIO_EXT = {".wav", ".ogg", ".mp3", ".flac", ".aiff"}
FONT_EXT = {".ttf", ".otf", ".woff", ".woff2"}
ANIM_EXT = {".anim", ".controller", ".overridecontroller", ".playable", ".mask"}
# Unity-only file types: these carry no meaning outside Unity.
UNITY_ONLY_EXT = {".prefab", ".mat", ".asset", ".unity", ".shader", ".shadergraph",
                  ".controller", ".overridecontroller", ".anim", ".playable",
                  ".mask", ".cs", ".shadervariants", ".cginc", ".hlsl", ".compute"}
# Formats an artist can open and change directly.
SOURCE_EXT = {".blend", ".psd", ".ai", ".svg", ".eps", ".fbx", ".obj", ".dae"}

ENGINES = ("unity", "godot", "unreal", "blender", "web")


def kind_of(ext: str) -> str:
    if ext in MODEL_EXT:
        return "model"
    if ext in ANIM_EXT:
        return "animation"
    if ext in TEXTURE_EXT:
        return "texture"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in FONT_EXT:
        return "font"
    if ext in UNITY_ONLY_EXT:
        return "unity-file"
    return "other"


def engines_for(ext: str, pack: dict) -> set[str]:
    """Which engines can actually consume this file.

    Two things gate it. Format: a .prefab or .shader means nothing outside
    Unity, while .fbx/.png/.wav open anywhere. Delivery: a package still sitting
    in the Asset Store cache is a Unity archive -- you can extract the meshes
    and take them elsewhere, but it is not a drag-and-drop for another engine.
    """
    if ext in UNITY_ONLY_EXT:
        return {"unity"}
    usable = set(ENGINES)
    if ext in AUDIO_EXT or ext in FONT_EXT:
        return usable
    if ext == ".blend":
        # Blender-native; other engines need an export step first.
        return {"blender", "unity", "godot", "unreal"}
    if ext in {".stl", ".dae"}:
        usable.discard("web")
    return usable


def rights_for(pack: dict) -> dict:
    """Resolve the licence into the yes/no questions people actually ask."""
    lic = pack["licence"]
    cc0 = lic == "CC0-1.0"
    unknown = pack["transferable"] == "unknown"

    # Modification: every licence here allows it except where provenance is
    # unestablished. Chequered Ink is the one to watch -- fonts may be used and
    # embedded but not disassembled or reverse-engineered.
    alterable = not unknown
    if lic.startswith("Chequered Ink"):
        alterable = False

    return {
        "cc0": int(cc0),
        "alterable": int(alterable),
        "commercial": int(not unknown),
        "redistributable": int(pack["redistributable"] == "yes"),
        "public_repo": int(bool(pack["public_repo_safe"])),
        "open_source_ok": int(cc0),
        "ai_training_ok": int(cc0),
        "licence_unknown": int(unknown),
    }


def main() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    con.executescript(
        """
        CREATE TABLE asset (
            id INTEGER PRIMARY KEY,
            name TEXT, ext TEXT, kind TEXT, path TEXT,
            pack TEXT, publisher TEXT, licence TEXT, theme TEXT,
            imported INT, size_mb REAL, pack_path TEXT,
            subject TEXT, context TEXT, variant TEXT, display TEXT,
            generic_name INT, prefab TEXT, mesh TEXT,
            is_ui INT, is_motion INT, is_modular INT, is_source INT,
            unity INT, godot INT, unreal INT, blender INT, web INT,
            cc0 INT, alterable INT, commercial INT, redistributable INT,
            public_repo INT, open_source_ok INT, ai_training_ok INT,
            licence_unknown INT
        );
        CREATE VIRTUAL TABLE asset_fts USING fts5(
            name, subject, folder, pack, publisher, theme, labels, related,
            content='', tokenize="unicode61 tokenchars '_-'"
        );
        """
    )

    rows = []
    fts = []
    seen: set[tuple] = set()
    for pack in catalog["packs"]:
        pack_tokens = tokens(pack["pack"]) | tokens(pack.get("theme", ""))
        rights = rights_for(pack)
        files = pack.get("all_files") or pack.get("model_files") or []

        # Index the pack's prefabs and meshes by subject first, so a texture
        # can say which prefab it dresses. A prefab name is usually the most
        # human-readable label in a Unity pack -- `Blaster.prefab` explains
        # `Blaster_MetallicSmoothness.png` far better than the filename does.
        prefab_by_subject: dict[str, str] = {}
        mesh_by_subject: dict[str, str] = {}
        for rel in files:
            p = Path(rel)
            ext = p.suffix.lower()
            if ext == ".prefab":
                prefab_by_subject.setdefault(subject_of(p.stem).lower(), p.stem)
            elif ext in MODEL_EXT:
                mesh_by_subject.setdefault(subject_of(p.stem).lower(), p.stem)

        for rel in files:
            p = Path(rel)
            ext = p.suffix.lower()
            kind = kind_of(ext)
            if kind == "other":
                continue
            # The same pack can appear twice -- imported and still cached. Keep
            # both (the paths differ) but not exact duplicates within one pack.
            key = (pack["pack"], pack.get("imported"), rel)
            if key in seen:
                continue
            seen.add(key)

            labels = label_path(rel, pack_tokens)
            eng = engines_for(ext, pack)
            folder = p.parent.as_posix()
            ctx = describe(rel, pack["pack"], kind)
            key_subject = ctx["subject"].lower()
            prefab = prefab_by_subject.get(key_subject, "")
            mesh = mesh_by_subject.get(key_subject, "")
            # Don't tell a prefab it relates to itself.
            if ext == ".prefab":
                prefab = ""
            if ext in MODEL_EXT:
                mesh = ""

            rows.append(
                (
                    p.stem, ext, kind, rel,
                    pack["pack"], pack["author"], pack["licence"],
                    pack.get("theme", ""),
                    int(bool(pack.get("imported"))), pack.get("size_mb") or 0.0,
                    pack["path"],
                    ctx["subject"], ctx["context"], ctx["variant"],
                    ctx["display"], ctx["generic_name"], prefab, mesh,
                    int("ui" in labels), int("motion" in labels),
                    int("modular" in labels), int(ext in SOURCE_EXT),
                    *(int(e in eng) for e in ENGINES),
                    rights["cc0"], rights["alterable"], rights["commercial"],
                    rights["redistributable"], rights["public_repo"],
                    rights["open_source_ok"], rights["ai_training_ok"],
                    rights["licence_unknown"],
                )
            )
            fts.append(
                (
                    p.stem.replace("_", " ").replace("-", " "),
                    ctx["subject"].replace("_", " ").replace("-", " "),
                    # Folder words are searchable in their own right: a texture
                    # called `1.png` is findable as "menu button" because its
                    # folder says so.
                    (ctx["context"] + " " + folder).replace("/", " ")
                    .replace(">", " ").replace("_", " "),
                    pack["pack"], pack["author"], pack.get("theme", ""),
                    " ".join(sorted(labels)) + " " + kind,
                    f"{prefab} {mesh} {ctx['variant']}".strip(),
                )
            )

    con.executemany(
        "INSERT INTO asset (name,ext,kind,path,pack,publisher,licence,theme,"
        "imported,size_mb,pack_path,subject,context,variant,display,"
        "generic_name,prefab,mesh,is_ui,is_motion,is_modular,is_source,"
        "unity,godot,unreal,blender,web,cc0,alterable,commercial,"
        "redistributable,public_repo,open_source_ok,ai_training_ok,"
        "licence_unknown) VALUES (" + ",".join(["?"] * 35) + ")",
        rows,
    )
    con.executemany(
        "INSERT INTO asset_fts (rowid,name,subject,folder,pack,publisher,theme,"
        "labels,related) VALUES (?,?,?,?,?,?,?,?,?)",
        [(i + 1, *f) for i, f in enumerate(fts)],
    )
    for col in ("kind", "pack", "licence", "subject", "is_ui", "is_motion",
                "is_modular"):
        con.execute(f"CREATE INDEX idx_{col} ON asset({col})")
    con.execute(
        "CREATE TABLE meta (built TEXT, assets INT, packs INT, source TEXT)"
    )
    con.execute(
        "INSERT INTO meta VALUES (?,?,?,?)",
        (date.today().isoformat(), len(rows), len(catalog["packs"]), str(CATALOG)),
    )
    con.commit()

    print(f"indexed {len(rows)} assets from {len(catalog['packs'])} packs")
    for kind, n in con.execute(
        "SELECT kind, COUNT(*) FROM asset GROUP BY kind ORDER BY 2 DESC"
    ):
        print(f"   {n:7} {kind}")
    for lbl in ("is_ui", "is_motion", "is_modular", "is_source"):
        n = con.execute(f"SELECT COUNT(*) FROM asset WHERE {lbl}=1").fetchone()[0]
        print(f"   {n:7} {lbl}")
    con.close()
    print(f"wrote {DB} ({DB.stat().st_size / 1048576:.1f} MB)")


if __name__ == "__main__":
    main()
