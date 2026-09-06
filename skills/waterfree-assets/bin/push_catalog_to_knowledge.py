"""Push the asset catalog into the WaterFree global knowledge base.

One entry per pack, plus one master index entry. Idempotent by way of the
store's SHA-256 code dedup: re-running after a rebuild only adds entries whose
model list actually changed.

Run after build_asset_catalog.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import assetpaths

CATALOG = assetpaths.CATALOG
SOURCE_REPO = str(assetpaths.HOME).replace(chr(92), '/')


def run_add(args: list[str]) -> dict:
    proc = subprocess.run(
        ["waterfree", "knowledge", "add", *args],
        capture_output=True,
        text=True,
        shell=True,
    )
    if proc.returncode != 0:
        print(f"  ! failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"  ! unparseable output: {proc.stdout[:200]}")
        return {}


def theme_tags(theme: str) -> list[str]:
    """Turn the comma-separated theme blurb into a few short search tags."""
    words = [w.strip().replace(" ", "-") for w in theme.split(",")]
    return [w for w in words if w][:5]


# licence string -> (short tag, transferability tag, taxonomy vendor segment)
LICENCE_TAGS = {
    "CC0-1.0": ("cc0", "engine-portable", "cc0"),
    "Unity Asset Store EULA (Restricted Assets)": (
        "unity-eula", "ship-built-only", "unity-asset-store"),
    "Quaternius Asset License (QAL) v1.0": (
        "quaternius-qal", "ship-built-only", "quaternius"),
    "Sonniss #GameAudioGDC Bundle EULA (royalty-free)": (
        "sonniss-eula", "royalty-free", "audio"),
    "AlkaKrab Music License (royalty-free)": (
        "alkakrab-licence", "royalty-free", "audio"),
    "Chequered Ink All Fonts Pack licence (paid, small-business tier)": (
        "chequered-ink-licence", "non-transferable", "fonts"),
    "Clembod free-asset licence": (
        "clembod-licence", "ship-built-only", "clembod"),
    "UNKNOWN -- no licence file present": (
        "licence-unknown", "unverified", "unverified"),
}


def slugify(name: str) -> str:
    keep = [c if (c.isalnum() or c in " -") else " " for c in name.lower()]
    return "-".join("".join(keep).split())


def add_pack(p: dict) -> None:
    cc0 = p["licence"] == "CC0-1.0"
    slug = slugify(p["pack"])
    lic_short, transfer_tag, vendor = LICENCE_TAGS.get(
        p["licence"], ("licence-unknown", "unverified", "unverified")
    )
    # Kenney and Quaternius are both CC0; keep them apart in the taxonomy.
    if cc0:
        vendor = "kenney" if p["pack"].startswith("kenney") else (
            "quaternius" if "Quaternius" in p["pack"] else "cc0")

    imported = p.get("imported", True)
    if not imported:
        # Cached packages get their own branch of the taxonomy, keyed by
        # publisher, so browsing assets/unity-cache lists what is downloadable
        # without wading through what is already extracted.
        hierarchy = f"assets/unity-cache/{slugify(p['author'])}/{slug}"
        title = f"Unimported Unity package: {p['pack']} ({p['author']})"
    else:
        hierarchy = f"assets/packs/{vendor}/{slug}"
        title = (
            f"Asset pack: {p['pack']} ({p['licence']}) -- "
            f"{p['theme'].split(',')[0]}"
        )

    body = [
        f"PACK:            {p['pack']}",
        "STATUS:          "
        + (
            "IMPORTED -- files are on disk at the path below"
            if imported
            else "DOWNLOADED BUT NOT IMPORTED -- .unitypackage in the Asset Store "
            "cache. Import via Package Manager > My Assets before use."
        ),
        f"PATH:            {p['path']}",
        f"AUTHOR:          {p['author']}",
        f"SOURCE:          {p['source']}",
        f"THEME:           {p['theme']}",
        "",]
    if not imported:
        body += [
            f"STORE CATEGORY:  {p.get('store_category', '')}",
            f"DOWNLOAD SIZE:   {p.get('size_mb', 0)} MB (downloaded {p.get('downloaded', '?')})",
            "",
        ]
    body += [
        f"LICENCE:         {p['licence']}",
        f"                 {p['licence_name']}",
        f"EVIDENCE:        {p['licence_evidence']}",
        f"ATTRIBUTION:     {'REQUIRED' if p['attribution_required'] else 'not required'}"
        f" -- {p['attribution_note']}",
        "",
        f"ENGINE:          {p['engine']}",
        f"                 {p['engine_note']}",
        f"TRANSFERABLE:    {p['transferable']}",
        f"                 {p['transferable_note']}",
        f"REDISTRIBUTE:    {p['redistributable']} (shipping the raw asset files to others)",
        f"PUBLIC REPO:     {'safe to commit' if p['public_repo_safe'] else 'DO NOT COMMIT -- EULA violation'}",
    ]
    if p.get("naming_prefix"):
        body.append(f"FILE PREFIX:     {p['naming_prefix']}_")
    if p.get("note"):
        body.append(f"NOTE:            {p['note']}")
    if p.get("extra_restrictions"):
        body.append("")
        body.append("WATCH OUT FOR:")
        for r in p["extra_restrictions"]:
            body.append(f"  * {r}")

    counts = ", ".join(f"{v} {k}" for k, v in sorted(p["counts"].items()))
    body += [
        "",
        f"CONTENTS:        {counts or 'nothing indexed'}",
        f"                 {p['distinct_object_count']} distinct {p['primary_kind']}s "
        f"({p['unique_model_count']} files before folding variants"
        + (f", plus {p['collider_mesh_count']} mesh colliders" if p["collider_mesh_count"] else "")
        + ")",
        f"FORMATS:         {', '.join(p['extensions']) or 'none'}",
        "",
        "FULL PER-FILE INDEX: "
        + (
            "C:/Projects/itch_assets/asset-catalog.json"
            if imported
            else "C:/Projects/itch_assets/unity-cache-catalog.json"
        ),
        "",
        f"-- {p['primary_kind'].upper()} INDEX (search this list for a specific asset) --",
    ]
    body += p["distinct_objects"]

    tags = [
        "asset-catalog",
        lic_short,
        vendor,
        transfer_tag,
        p["primary_kind"],
        *theme_tags(p["theme"]),
    ]
    if not imported:
        tags = ["unity-cache", "not-imported", *tags]

    kind_word = {
        "model": "3D models",
        "audio": "audio files",
        "font": "fonts",
        "texture": "2D images",
        "none": "assets",
    }[p["primary_kind"]]

    if p["transferable"] == "unknown":
        verdict = (
            "LICENCE NOT ESTABLISHED -- no licence file on disk. Treat as fully "
            "restricted until the purchase receipt or contract is checked."
        )
    elif cc0:
        verdict = (
            "public domain, usable in any engine, redistributable, safe in a public repo."
        )
    else:
        verdict = (
            "usable in any engine, but only shipped inside a built product; the raw "
            "files must never be redistributed or committed to a public repo."
        )
        if p.get("extra_restrictions"):
            verdict += f" {len(p['extra_restrictions'])} extra restrictions apply -- see body."

    if imported:
        status_line = ""
    else:
        status_line = (
            f"OWNED BUT NOT IMPORTED -- a {p.get('size_mb', 0)} MB .unitypackage "
            "in the Unity Asset Store download cache; import it via Package "
            "Manager > My Assets before the files exist in any project. "
        )

    desc = (
        f"{p['pack']} by {p['author']}: {status_line}"
        f"{p['distinct_object_count']} distinct {kind_word} themed around "
        f"{p['theme']}. Licence {p['licence']} -- {verdict} "
        "Body holds the full asset-name index for searching."
    )

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write("\n".join(body))
        code_file = fh.name

    args = [
        "--title", title,
        "--description", desc,
        "--code-file", code_file,
        # The store only accepts pattern/utility/style/api_usage/convention;
        # a licence rule is closest to a convention.
        "--snippet-type", "convention",
        "--source-repo", SOURCE_REPO,
        "--source-file",
        "asset-catalog.json" if imported else "unity-cache-catalog.json",
        "--hierarchy-path", hierarchy,
        "--context",
        f"Licence summary is a good-faith reading of {p['licence_evidence']}, not legal "
        f"advice. Verify before a commercial release. Regenerate this entry with "
        f"C:/Projects/itch_assets/build_asset_catalog.py then push_catalog_to_knowledge.py.",
    ]
    for t in tags:
        args += ["--tag", t]

    res = run_add(args)
    Path(code_file).unlink(missing_ok=True)
    status = "added" if res.get("added", True) else "duplicate, skipped"
    print(f"  {p['pack']}: {status}")


def add_index(catalog: dict) -> None:
    imported = [p for p in catalog["packs"] if p.get("imported")]
    cached = [p for p in catalog["packs"] if not p.get("imported")]

    rows = [
        "OWNED GAME ASSETS -- MASTER INDEX",
        f"generated {catalog['generated']}",
        "",
        "Catalog files:",
        "  C:/Projects/itch_assets/ASSET-CATALOG.md          human-readable",
        "  C:/Projects/itch_assets/asset-catalog.json        per-file index",
        "  C:/Projects/itch_assets/unity-cache-catalog.json  per-file index of",
        "      the Unity Asset Store download cache (owned, not imported)",
        "  C:/Projects/itch_assets/build_asset_catalog.py       regenerate",
        "  C:/Projects/itch_assets/build_unity_cache_index.py   rescan the cache",
        "",
        "TWO KINDS OF ENTRY:",
        f"  {len(imported):3} packs are IMPORTED -- files on disk, usable now.",
        f"  {len(cached):3} packages are DOWNLOADED BUT NOT IMPORTED -- licensed to",
        "      this Unity account and sitting in the Asset Store cache as",
        "      .unitypackage archives. Import via Package Manager > My Assets.",
        "      Check these before buying anything: it is a large library.",
        "",
        "SIX LICENCE CLASSES. Only one of them lets you redistribute anything.",
        "",
        "  CC0-1.0 (Kenney packs, most Quaternius packs)",
        "    Public domain. Any engine, any use, resell the raw files if you like,",
        "    commit them to a public repo. Credit anyway; it costs nothing.",
        "",
        "  Unity Asset Store EULA / Restricted Assets (SICS TOON Series + free Unity packs)",
        "    NOT engine-locked -- you may build in Godot/Unreal/anything. The limit is",
        "    on distribution form: ship only merged into an interactive product where",
        "    end users cannot extract the source files. Never redistribute the assets,",
        "    never commit them to a public repo, no NFT use, no AI training data, no",
        "    transfer to a client. Unity ShaderLab shaders do not port out of Unity.",
        "",
        "  Quaternius Asset License (QAL) v1.0 (newer Quaternius packs only)",
        "    Any engine, commercial, no credit owed. Only bar is repackaging them as",
        "    a standalone asset pack. NOTE: Quaternius switched from CC0 to QAL part",
        "    way through -- read the License.txt in each pack, never assume.",
        "",
        "  Sonniss #GameAudioGDC Bundle EULA (all Sonniss audio)",
        "    Royalty-free, perpetual, unlimited projects, commercial, no attribution.",
        "    Cannot sell the sounds as-is. NO AI TRAINING. English law.",
        "",
        "  AlkaKrab Music License (AlkaKrab tracks)",
        "    Commercial at any revenue. NO OPEN SOURCE without written permission --",
        "    the licence calls this case out by name. No remix/sampling, no Spotify.",
        "",
        "  Chequered Ink All Fonts Pack (paid licence, small-business tier)",
        "    Personal AND commercial, but non-transferable and capped at 5 devices.",
        "    ELIGIBILITY IS CONDITIONAL on staying under 20 staff and $2M turnover.",
        "    Fonts must be extraction-protected in a shipped game, and players must",
        "    not be able to set their own text in them. AI training carries a",
        "    $10,000-per-infringement clause. Keep the receipt: the agreement says",
        "    holding the licence text is not itself proof of eligibility.",
        "",
        "  Clembod free-asset licence (Warrior free set)",
        "    Personal and commercial granted outright, modification fine, credit",
        "    appreciated. No redistribution or resale.",
        "",
        "  UNKNOWN -- no licence file on disk (SunGraphica UI packs, Goblin Chess)",
        "    Treat as fully restricted until the receipt or contract is checked.",
        "",
        "== IMPORTED PACKS (files on disk, usable now) ==",
        "",
        f"{'PACK':38} {'LICENCE':12} {'REDIST':8} {'PUBREPO':8} OBJECTS",
    ]
    short = {
        "CC0-1.0": "CC0",
        "Unity Asset Store EULA (Restricted Assets)": "Unity-EULA",
        "Quaternius Asset License (QAL) v1.0": "QAL-1.0",
        "Sonniss #GameAudioGDC Bundle EULA (royalty-free)": "Sonniss",
        "AlkaKrab Music License (royalty-free)": "AlkaKrab",
        "Chequered Ink All Fonts Pack licence (paid, small-business tier)": "ChequeredInk",
        "Clembod free-asset licence": "Clembod",
        "UNKNOWN -- no licence file present": "UNKNOWN",
    }
    for p in imported:
        lic = short.get(p["licence"], "?")
        rows.append(
            f"{p['pack'][:38]:38} {lic:12} "
            f"{p['redistributable']:8} {'yes' if p['public_repo_safe'] else 'NO':8} "
            f"{p['distinct_object_count']}"
        )

    if cached:
        gb = sum(p.get("size_mb", 0) for p in cached) / 1024
        rows += [
            "",
            "== OWNED BUT NOT IMPORTED -- Unity Asset Store download cache ==",
            f"   {len(cached)} packages, {gb:.1f} GB, all Unity Asset Store EULA.",
            "   Import via Package Manager > My Assets. Look here before buying.",
            "",
            f"{'PACKAGE':44} {'PUBLISHER':24} {'MB':>7} {'KIND':8} OBJECTS",
        ]
        for p in sorted(cached, key=lambda x: (x["author"].lower(), x["pack"].lower())):
            rows.append(
                f"{p['pack'][:44]:44} {p['author'][:24]:24} "
                f"{p.get('size_mb', 0):7.0f} {p['primary_kind']:8} "
                f"{p['distinct_object_count']}"
            )

    total = sum(p["distinct_object_count"] for p in catalog["packs"])
    rows += [
        "",
        f"TOTAL: {catalog['pack_count']} packs ({len(imported)} imported, "
        f"{len(cached)} cached), {total} distinct assets",
    ]

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write("\n".join(rows))
        code_file = fh.name

    args = [
        "--title", "Owned game asset packs: master index, licences and transferability",
        "--description",
        f"Start here before sourcing art. Indexes {catalog['pack_count']} owned asset "
        f"packs covering {total} distinct assets -- 3D models, audio, fonts and 2D UI "
        "art -- across six licence classes: CC0 (public domain), Unity Asset Store "
        "EULA, Quaternius QAL, Sonniss audio EULA, AlkaKrab music, and the paid "
        "Chequered Ink font licence, plus some with no licence file at all. Answers "
        "'what do I own', 'may I use it outside Unity', 'may this go in a public repo', "
        "'may this go in an open-source game', and 'may I train a model on it' (no).",
        "--code-file", code_file,
        "--snippet-type", "convention",
        "--source-repo", SOURCE_REPO,
        "--source-file", "ASSET-CATALOG.md",
        "--hierarchy-path", "assets/packs",
        "--context",
        "Licence summaries are a good-faith reading of shipped licence files and "
        "published store terms, not legal advice; confirm before a commercial release. "
        "Related: the Kenney packs do not share a common scale -- search the knowledge "
        "base for 'Kenney asset packs do NOT share a scale' before mixing them. "
        "Two packs still need their licence confirmed from a purchase receipt: the "
        "SunGraphica UI collections (gamedevmarket.net) and Goblin Chess.",
        "--tag", "asset-catalog",
        "--tag", "licensing",
        "--tag", "cc0",
        "--tag", "unity-eula",
        "--tag", "gamedev-assets",
        "--tag", "index",
    ]
    res = run_add(args)
    Path(code_file).unlink(missing_ok=True)
    print(f"  MASTER INDEX: {'added' if res.get('added', True) else 'duplicate, skipped'}")


def purge_previous() -> None:
    """Delete catalog entries from an earlier run.

    The store dedups on a hash of the body, so a rebuild that changes any pack's
    text would leave the stale version sitting alongside the new one. Only
    entries this script wrote (matched on source_repo) are removed.
    """
    seen: dict[str, str] = {}
    for query in (
        "asset pack licence transferability",
        "owned game asset packs master index",
        "unimported unity package asset store cache",
    ):
        proc = subprocess.run(
            ["waterfree", "knowledge", "search", query, "--limit", "200"],
            capture_output=True, text=True, shell=True,
        )
        if proc.returncode != 0:
            continue
        for e in json.loads(proc.stdout).get("entries", []):
            if e.get("source_repo") == SOURCE_REPO:
                seen[e["id"]] = e["title"]

    for entry_id, title in seen.items():
        proc = subprocess.run(
            ["waterfree", "knowledge", "delete", entry_id],
            capture_output=True, text=True, shell=True,
        )
        ok = "deleted" if proc.returncode == 0 else f"FAILED ({proc.returncode})"
        print(f"  purge {ok}: {title[:70]}")
    print(f"  purged {len(seen)} stale entries\n")


def main() -> None:
    if not CATALOG.exists():
        sys.exit(f"missing {CATALOG} -- run build_asset_catalog.py first")
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    purge_previous()
    add_index(catalog)
    for p in catalog["packs"]:
        add_pack(p)


if __name__ == "__main__":
    main()
