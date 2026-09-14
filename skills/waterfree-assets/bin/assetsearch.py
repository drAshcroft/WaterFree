"""Search the owned-asset database by content, engine and rights.

    assetsearch "sci fi gun, godot, alterable"
    assetsearch "fantasy, button, unity"
    assetsearch "barn, modular, cc0" --limit 40
    assetsearch "idle animation, unity" --json

A query is a comma- or space-separated mix of three kinds of term, and you do
not have to say which is which:

  ENGINE   unity | godot | unreal | blender | web
           Filters to assets that engine can actually consume. A .prefab or
           .shader is Unity-only; .fbx/.png/.wav open anywhere.

  RIGHTS   alterable, commercial, redistributable, public-repo, open-source,
           cc0, ai-training, free (= cc0), safe (= known licence)
           Filters on what the pack's licence permits.

  KIND     model/3d, texture/2d/sprite, audio/sound/sfx/music, font,
           ui/gui/hud/icon/button/menu, motion/animation/animated,
           modular/tileset, source (editable .blend/.psd/.fbx)

  TEXT     everything else -- matched against the asset name, its folder, its
           pack, publisher and theme.

Anything unrecognised is treated as text, so plain searches still work.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import assetpaths

DB = assetpaths.DB

ENGINE_WORDS = {
    "unity": "unity", "godot": "godot", "unreal": "unreal", "ue5": "unreal",
    "blender": "blender", "web": "web", "threejs": "web", "three": "web",
    "browser": "web",
}

RIGHTS_WORDS = {
    "alterable": "alterable", "modifiable": "alterable", "editable": "alterable",
    "modify": "alterable", "alter": "alterable",
    "commercial": "commercial", "sellable": "commercial",
    "redistributable": "redistributable", "resell": "redistributable",
    "publicrepo": "public_repo", "public-repo": "public_repo",
    "github": "public_repo", "opensource": "open_source_ok",
    "open-source": "open_source_ok", "oss": "open_source_ok",
    "cc0": "cc0", "publicdomain": "cc0", "free": "cc0",
    "aitraining": "ai_training_ok", "ai-training": "ai_training_ok",
}

KIND_WORDS = {
    "model": "kind", "models": "kind", "3d": "kind", "mesh": "kind",
    "prop": "kind", "props": "kind",
    "texture": "kind", "textures": "kind", "2d": "kind", "sprite": "kind",
    "sprites": "kind", "image": "kind", "images": "kind",
    "audio": "kind", "sound": "kind", "sounds": "kind", "sfx": "kind",
    "music": "kind", "font": "kind", "fonts": "kind", "typeface": "kind",
}
KIND_MAP = {
    "model": "model", "models": "model", "3d": "model", "mesh": "model",
    "prop": "model", "props": "model",
    "texture": "texture", "textures": "texture", "2d": "texture",
    "sprite": "texture", "sprites": "texture", "image": "texture",
    "images": "texture",
    "audio": "audio", "sound": "audio", "sounds": "audio", "sfx": "audio",
    "music": "audio", "font": "font", "fonts": "font", "typeface": "font",
}

LABEL_WORDS = {
    "ui": "is_ui", "gui": "is_ui", "hud": "is_ui", "icon": "is_ui",
    "icons": "is_ui", "button": "is_ui", "buttons": "is_ui", "menu": "is_ui",
    "interface": "is_ui",
    "motion": "is_motion", "animation": "is_motion", "animations": "is_motion",
    "animated": "is_motion", "anim": "is_motion", "clip": "is_motion",
    "modular": "is_modular", "tileset": "is_modular", "kit": "is_modular",
    "tiles": "is_modular",
    "source": "is_source", "sourcefile": "is_source",
}

STATUS_WORDS = {"imported": 1, "installed": 1, "cached": 0, "unimported": 0,
                "notimported": 0}

# Multi-word concepts that must survive tokenisation. Applied to the raw query
# before splitting, so "sci fi", "sci-fi" and the typo "sci fy" all land on the
# same token as the filenames that spell it "SciFi".
PHRASES = [
    (r"sci[\s_\-]*f[iy]\b", "scifi"),
    (r"\bscience[\s_\-]*fiction\b", "scifi"),
    (r"\btop[\s_\-]*down\b", "topdown"),
    (r"\blow[\s_\-]*poly\b", "lowpoly"),
    (r"\bfirst[\s_\-]*person\b", "fps"),
    (r"\bpost[\s_\-]*apocalyptic\b", "apocalyptic"),
]

# A search is for a concept, not a filename. Nobody names a mesh "gun" when
# they could name it "Rifle_01", so a bare term has to reach its neighbours.
SYNONYMS = {
    "gun": ["gun", "rifle", "pistol", "weapon", "blaster", "shotgun", "cannon",
            "revolver", "firearm", "musket"],
    "weapon": ["weapon", "sword", "axe", "gun", "rifle", "blade", "dagger",
               "spear", "bow", "hammer", "mace"],
    "scifi": ["scifi", "sci", "futuristic", "space", "cyber", "tech", "hologram"],
    "car": ["car", "vehicle", "auto", "sedan", "truck", "van"],
    "tree": ["tree", "pine", "oak", "palm", "foliage", "vegetation"],
    "house": ["house", "building", "home", "cottage", "hut", "cabin"],
    "monster": ["monster", "creature", "enemy", "beast", "demon", "goblin"],
    "character": ["character", "hero", "humanoid", "avatar", "npc", "person"],
    "rock": ["rock", "stone", "boulder", "cliff"],
    "chest": ["chest", "crate", "box", "barrel", "container"],
}


def expand(term: str) -> list[str]:
    return SYNONYMS.get(term, [term])


def normalise(query: str) -> str:
    out = query.lower()
    for pattern, repl in PHRASES:
        out = re.sub(pattern, repl, out)
    return out


def parse(query: str) -> dict:
    """Split a query into facets. Unrecognised words become free text."""
    raw = [t for t in re.split(r"[,\s]+", normalise(query).strip()) if t]
    facets: dict = {
        "engines": [], "rights": [], "kinds": [], "labels": [],
        "status": None, "text": [],
    }
    for token in raw:
        key = token.lower().strip()
        flat = key.replace("_", "").replace("-", "")
        if key in ENGINE_WORDS or flat in ENGINE_WORDS:
            facets["engines"].append(ENGINE_WORDS.get(key) or ENGINE_WORDS[flat])
        elif key in RIGHTS_WORDS or flat in RIGHTS_WORDS:
            facets["rights"].append(RIGHTS_WORDS.get(key) or RIGHTS_WORDS[flat])
        elif key in KIND_MAP:
            facets["kinds"].append(KIND_MAP[key])
        elif key in LABEL_WORDS:
            facets["labels"].append(LABEL_WORDS[key])
        elif flat in STATUS_WORDS:
            facets["status"] = STATUS_WORDS[flat]
        else:
            facets["text"].append(key)
    return facets


def build_sql(f: dict, limit: int, strict: str = "strong") -> tuple[str, list]:
    """Build the query at one of three precision tiers.

    strong  every term must match a field describing the asset itself
    all     every term must match somewhere, pack name and theme included
    loose   any term may match

    The caller walks down the tiers until something comes back, so a precise
    query stays precise while a vague or slightly-wrong one still answers."""
    where, params = [], []

    for eng in set(f["engines"]):
        where.append(f"a.{eng} = 1")
    for right in set(f["rights"]):
        where.append(f"a.{right} = 1")
    for lbl in set(f["labels"]):
        where.append(f"a.{lbl} = 1")
    if f["kinds"]:
        ks = sorted(set(f["kinds"]))
        # "model" should also surface animation clips riding on meshes.
        where.append("(" + " OR ".join("a.kind = ?" for _ in ks) + ")")
        params += ks
    if f["status"] is not None:
        where.append("a.imported = ?")
        params.append(f["status"])

    if f["text"]:
        # Every text term must appear somewhere in the row, but any field will
        # do -- "sci fi gun" matches a name of SciFi_Gun or a sci-fi pack
        # containing a gun.
        # Each term becomes an OR-group of its synonyms; the groups are then
        # ANDed (strict) or ORed (fallback).
        # Three tiers of precision. "strong" only looks at fields that describe
        # the asset itself -- its name, what it depicts, its folder, the prefab
        # it belongs to. Pack name and theme are excluded here on purpose: the
        # Toon Farm Pack theme mentions "barn", which would otherwise make all
        # 5,000 of its files equally good matches for "barn".
        cols = "{name subject folder related}" if strict == "strong" else ""
        groups = [
            "(" + " OR ".join(f'{cols}:"{s}"*' if cols else f'"{s}"*'
                              for s in expand(t)) + ")"
            for t in f["text"]
        ]
        match = (" OR " if strict == "loose" else " AND ").join(groups)
        sql = (
            "SELECT a.*, bm25(asset_fts, 10.0, 8.0, 4.0, 2.0, 1.0, 1.5, 3.0, 5.0) AS rank "
            "FROM asset_fts JOIN asset a ON a.id = asset_fts.rowid "
            "WHERE asset_fts MATCH ?"
        )
        params.insert(0, match)
        if where:
            sql += " AND " + " AND ".join(where)
        sql += " ORDER BY rank LIMIT ?"
    else:
        sql = "SELECT a.*, 0 AS rank FROM asset a"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY a.imported DESC, a.pack, a.name LIMIT ?"
    params.append(limit)
    return sql, params


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="assetsearch", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("query", nargs="+")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--packs", action="store_true",
                    help="summarise by pack instead of listing every asset")
    ap.add_argument("--subjects", action="store_true",
                    help="group files by the thing they depict (collapses "
                         "texture map sets, LODs and resolution variants)")
    ap.add_argument("--semantic-audio", action="store_true",
                    help="delegate to the local CLAP enrichment index")
    ap.add_argument("--semantic-visual", action="store_true",
                    help="delegate to the local SigLIP 2 enrichment index")
    args = ap.parse_args()

    if not DB.exists():
        print(f"no index at {DB} -- run build_search_index.py", file=sys.stderr)
        return 1

    query = " ".join(args.query)
    if args.semantic_audio or args.semantic_visual:
        if args.packs or args.subjects or (args.semantic_audio and args.semantic_visual):
            ap.error("semantic modes cannot be combined with --packs, --subjects, or each other")
        central = Path(r"C:\Projects\itch_assets\assetsearch.py")
        python = Path(r"C:\Projects\.local\Scripts\python.exe")
        if not central.is_file():
            print(f"semantic asset index not found at {central}", file=sys.stderr)
            return 1
        mode = "--semantic-audio" if args.semantic_audio else "--semantic-visual"
        command = [str(python if python.is_file() else sys.executable), str(central),
                   query, mode, "--limit", str(args.limit)]
        if args.json:
            command.append("--json")
        return subprocess.run(command, check=False).returncode

    f = parse(query)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cap = args.limit if not (args.packs or args.subjects) else 5000
    tier = ""
    try:
        hits = []
        for mode, note in (
            ("strong", ""),
            ("all", "no direct name match; also matching pack name and theme"),
            ("loose", "no asset matched every term; showing any-term matches"),
        ):
            sql, params = build_sql(f, cap, strict=mode)
            hits = [dict(r) for r in con.execute(sql, params)]
            if hits:
                tier = note
                break
    except sqlite3.OperationalError as exc:
        print(f"bad query: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"query": query, "facets": f, "results": hits}, indent=2))
        return 0

    applied = []
    if f["engines"]:
        applied.append("engine=" + "+".join(sorted(set(f["engines"]))))
    if f["rights"]:
        applied.append("rights=" + "+".join(sorted(set(f["rights"]))))
    if f["kinds"]:
        applied.append("kind=" + "+".join(sorted(set(f["kinds"]))))
    if f["labels"]:
        applied.append("is=" + "+".join(s[3:] for s in sorted(set(f["labels"]))))
    if f["status"] is not None:
        applied.append("imported" if f["status"] else "cached")
    if f["text"]:
        applied.append("text=" + " ".join(f["text"]))

    total = con.execute(
        sql.replace("SELECT a.*, bm25(asset_fts, 10.0, 8.0, 4.0, 2.0, 1.0, 1.5, 3.0, 5.0) AS rank",
                    "SELECT COUNT(*) AS n")
           .replace("SELECT a.*, 0 AS rank", "SELECT COUNT(*) AS n")
           .replace(" ORDER BY rank LIMIT ?", "")
           .replace(" ORDER BY a.imported DESC, a.pack, a.name LIMIT ?", ""),
        params[:-1],
    ).fetchone()[0]

    print(f"\n{query!r}  ->  {' | '.join(applied) or 'no filters'}")
    if tier:
        print(tier)
    print(f"{total} matching assets\n")

    if args.packs:
        by_pack: dict[tuple, int] = {}
        for h in hits:
            key = (h["pack"], h["publisher"], h["licence"], h["imported"],
                   h["pack_path"])
            by_pack[key] = by_pack.get(key, 0) + 1
        for (pack, pub, lic, imp, path), n in sorted(
            by_pack.items(), key=lambda kv: -kv[1]
        )[: args.limit]:
            tag = "imported" if imp else "CACHED - import first"
            print(f"  {n:5}  {pack[:42]:42} {pub[:20]:20} [{tag}]")
            print(f"         {lic[:70]}")
            print(f"         {path}")
        return 0

    if args.subjects:
        # Collapse a material's five map files, a mesh's LODs and a sprite's
        # resolutions into the one thing they all depict.
        groups: dict[tuple, dict] = {}
        for h in hits:
            key = (h["pack"], h["subject"])
            g = groups.setdefault(
                key, {"n": 0, "kinds": set(), "row": h, "prefab": "", "mesh": ""}
            )
            g["n"] += 1
            g["kinds"].add(h["kind"])
            g["prefab"] = g["prefab"] or h["prefab"]
            g["mesh"] = g["mesh"] or h["mesh"]
        for (pack, subject), g in list(groups.items())[: args.limit]:
            h = g["row"]
            tag = "" if h["imported"] else "  [CACHED - import first]"
            context = h["context"]
            where = f"  -  {context}" if context else ""
            print(f"  {subject[:44]:44} {'+'.join(sorted(g['kinds']))[:22]:22}"
                  f" {g['n']} file{'s' if g['n'] != 1 else ''}{tag}")
            print(f"      {pack}{where}")
            extra = []
            if g["prefab"]:
                extra.append(f"prefab: {g['prefab']}")
            if g["mesh"]:
                extra.append(f"mesh: {g['mesh']}")
            if h["variant"]:
                extra.append(h["variant"])
            if extra:
                print(f"      {' | '.join(extra)}")
        if len(groups) > args.limit:
            print(f"\n  ... {len(groups) - args.limit} more subjects")
        return 0

    for h in hits:
        flags = "".join(
            c for c, k in (("U", "is_ui"), ("M", "is_motion"),
                           ("K", "is_modular"), ("S", "is_source")) if h[k]
        ) or "-"
        tag = "" if h["imported"] else "  [CACHED]"
        print(f"  {h['display'][:46]:46} {h['kind']:10} {flags:4}{tag}")
        # The context line is the point: a filename alone rarely says what the
        # asset is, but pack + folder trail + the prefab it dresses does.
        bits = [h["pack"]]
        if h["context"]:
            bits.append(h["context"])
        if h["subject"].lower() != h["name"].lower():
            bits.append(f"part of {h['subject']}")
        if h["prefab"]:
            bits.append(f"prefab {h['prefab']}")
        elif h["mesh"]:
            bits.append(f"mesh {h['mesh']}")
        if h["variant"]:
            bits.append(h["variant"])
        print(f"      {'  -  '.join(bits)[:108]}")
        print(f"      {h['licence'][:52]}")
        print(f"      {h['path'][:110]}")
    if total > len(hits):
        print(f"\n  ... {total - len(hits)} more; raise --limit, "
              f"or add --subjects / --packs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
