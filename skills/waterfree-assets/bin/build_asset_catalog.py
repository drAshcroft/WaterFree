"""Build a searchable catalog of owned game assets.

Scans the known asset roots, records every individual model/texture/audio file,
and joins it against hand-verified licence metadata (read from the licence files
that ship with each pack, or from the store EULA where the pack ships none).

Outputs, both into this directory:
  asset-catalog.json  machine-readable, per-model index
  ASSET-CATALOG.md    human-readable summary table

Re-run after adding a pack. Add its licence row to PACKS below first -- a pack
with no row is reported as UNKNOWN licence rather than silently assumed free.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from asset_labels import label_pack

import assetpaths

OUT_DIR = assetpaths.HOME

# Extensions worth indexing individually, grouped by what they are.
MODEL_EXT = {".glb", ".gltf", ".fbx", ".obj", ".dae", ".stl", ".blend"}
TEXTURE_EXT = {".png", ".jpg", ".jpeg", ".psd", ".tga", ".exr"}
AUDIO_EXT = {".wav", ".ogg", ".mp3", ".flac", ".aiff"}
FONT_EXT = {".ttf", ".otf", ".woff", ".woff2"}

# Files Unity generates that are not assets in their own right.
SKIP_EXT = {".meta"}

# ---------------------------------------------------------------------------
# Licence metadata. Every field here was verified against a file on disk or a
# published store term -- see `licence_evidence` for which.
# ---------------------------------------------------------------------------

KENNEY_COMMON = {
    "licence": "CC0-1.0",
    "licence_name": "Creative Commons Zero 1.0 (public domain dedication)",
    "author": "Kenney",
    "source": "https://kenney.nl",
    "engine": "any",
    "engine_note": "Raw glTF/FBX/OBJ meshes, no engine-specific code. Import anywhere.",
    "transferable": "yes",
    "transferable_note": "No restriction. Use, modify, sublicense, sell, in any engine.",
    "redistributable": "yes",
    "public_repo_safe": True,
    "attribution_required": False,
    "attribution_note": "Kenney asks for credit but does not require it. Credit anyway.",
    "licence_evidence": "License.txt shipped in each pack directory",
}

SICS_COMMON = {
    "licence": "Unity Asset Store EULA (Restricted Assets)",
    "licence_name": "Unity Asset Store End User License Agreement, Restricted Asset tier",
    "author": "SICS Games",
    "source": "https://assetstore.unity.com  (contact: sics.production@gmail.com)",
    "engine": "unity-authored",
    "engine_note": (
        "Meshes/textures/animations are plain FBX+PNG and port to any engine. "
        "The custom toon/water/grass/fire shaders are Unity ShaderLab and do NOT "
        "port -- budget for rewriting them if leaving Unity."
    ),
    "transferable": "yes-with-conditions",
    "transferable_note": (
        "The EULA is not engine-locked: you may use these outside Unity. What is "
        "restricted is HOW you distribute, not WHICH engine you build in. Must ship "
        "merged into an interactive product where end users cannot extract the "
        "source assets."
    ),
    "redistributable": "no",
    "public_repo_safe": False,
    "attribution_required": False,
    "attribution_note": (
        "Not required, but note the packs ship NO licence file -- only install "
        "instructions. Keep your purchase receipt as the proof of licence."
    ),
    "licence_evidence": (
        "No licence file ships with these packs (verified: only _ReadFirst.txt "
        "install notes and Documentation/*.txt usage notes). Terms are the Unity "
        "Asset Store EULA accepted at purchase: https://unity.com/legal/as-terms"
    ),
}

UNITY_FREE_COMMON = dict(SICS_COMMON)
UNITY_FREE_COMMON.update(
    {
        "licence_evidence": (
            "No licence file ships with this pack. Free Asset Store downloads carry "
            "the same Unity Asset Store EULA as paid ones -- free means zero price, "
            "not a permissive licence."
        )
    }
)


def sics(theme: str, prefix: str = "", note: str = "") -> dict:
    d = dict(SICS_COMMON)
    d["theme"] = theme
    if prefix:
        d["naming_prefix"] = prefix
    if note:
        d["note"] = note
    return d


def kenney(theme: str, note: str = "") -> dict:
    d = dict(KENNEY_COMMON)
    d["theme"] = theme
    if note:
        d["note"] = note
    return d


PACKS: dict[str, dict] = {
    # --- Kenney, C:\Projects\kenny_assets -----------------------------------
    "kenney_car-kit": kenney("vehicles, cars, trucks, emergency, racing"),
    "kenney_city-kit-suburban_20": kenney(
        "suburban buildings, houses, roads, modular city"
    ),
    "kenney_cube-pets_1.0": kenney("animals, pets, cat, dog, characters, chunky"),
    "kenney_food-kit": kenney("food, kitchen, cooking, props, ingredients"),
    "kenney_furniture-kit": kenney(
        "furniture, interior, rooms, household, isometric",
        note="Dollhouse-scaled for isometric scenes; also ships Isometric/ and Side/ sprite renders.",
    ),
    "kenney_holiday-kit": kenney("christmas, winter, holiday, snow, decorations"),
    "kenney_mini-skate": kenney(
        "skatepark, ramps, half-pipe, modular",
        note="Built on a 1m modular grid.",
    ),
    "kenney_nature-kit": kenney(
        "trees, rocks, terrain, plants, cliffs, outdoors, nature"
    ),
    "kenney_prototype-textures": kenney(
        "greybox, prototype, grid textures, blockout",
        note="Textures only, no meshes.",
    ),
    # --- SICS Games TOON Series, C:\Projects\humble\humble\Assets -----------
    "Toon Adventure Island": sics(
        "tropical island, beach, palm trees, pirate, water, jungle", "TAI"
    ),
    "Toon City People": sics(
        "humanoid characters, civilians, customisable, blendshapes",
        "",
        "Mecanim-compatible rig -- works with any humanoid animation pack. "
        "Ships idle animations only. Fatness Levels blendshape scripts included.",
    ),
    "Toon Desert": sics("desert, dunes, cacti, canyon, western, arid", "TD"),
    "Toon Deserted Temples": sics(
        "ruins, temples, jungle, ancient, overgrown, archaeology", "TDT"
    ),
    "Toon Enchanted Meadow": sics(
        "fantasy meadow, mushrooms, flowers, whimsical, fairy", "TEM"
    ),
    "Toon Fantasy Nature": sics(
        "fantasy forest, trees, rocks, vegetation, water, fire", "TFF"
    ),
    "Toon Farm Pack": sics(
        "farm, barn, livestock, animals, crops, rural, tractor",
        "TFP",
        "Largest pack on hand by a wide margin, and the only one with a big "
        "animation library (see Documentation/Animated Animals.txt).",
    ),
    "Toon Gas Station": sics(
        "gas station, roadside, fuel pumps, americana",
        "",
        "NOT IMPORTED -- ships as .unitypackage files only. Import before use.",
    ),
    "Toon Series": sics(
        "city, vehicles, streets, urban props",
        "",
        "Umbrella folder containing Toon City plus Shared assets. "
        "Pivots centred at base; .L/.R suffixes mark animatable left/right parts.",
    ),
    "Toon Suburban Pack": sics(
        "suburban, houses, streets, gardens, residential",
        "",
        "NOT IMPORTED -- ships as .unitypackage files only. Import before use.",
    ),
    # --- Other Unity Asset Store packs -------------------------------------
    "Agarkova_CG": {
        **UNITY_FREE_COMMON,
        "author": "AgarkovaCG",
        "theme": "viking warrior, humanoid character, rigged, armour, fantasy",
        "note": "Store listing: 'Warrior viking with red hair and armor'.",
    },
    "CS - Signs Free": {
        **UNITY_FREE_COMMON,
        "author": "Covalence Studio by Z W Rethati",
        "theme": "road signs, US highway signs, street furniture, 2D sprites and 3D prefabs",
        "note": (
            "Free cut of 'US Road Signs Megapack'; the paid version adds 550 more "
            "signs. Usable as 2D sprites or 3D prefabs -- see Readme.txt."
        ),
    },
    "TARBO-TowerDefensePack": {
        **UNITY_FREE_COMMON,
        "author": "Tarbo Studios",
        "theme": "tower defense, towers, enemies, stylized levels, game kit",
        "licence_evidence": "License.pdf inside the pack, which defers to the Unity Asset Store EULA",
        "extra_restrictions": [
            "Publisher spells out the usual EULA limits: no copying, duplicating, "
            "reproducing, selling, reselling or trading the pack.",
            "The DEMO SCENES specifically may not be redistributed in any way.",
            "You may not hand a copy to another person -- including a friend -- for "
            "use in their own project. Each person needs their own licence.",
        ],
    },
    "Warrior free set": {
        "licence": "Clembod free-asset licence",
        "licence_name": "Clembod custom licence (License.txt shipped with the pack)",
        "author": "Clembod",
        "source": "https://clembod.itch.io  (Clembod@gmail.com, @Clembod)",
        "engine": "any",
        "engine_note": "2D sprite sheets and animations. The art ports anywhere.",
        "transferable": "yes-with-conditions",
        "transferable_note": (
            "Personal AND commercial use are both granted outright, and you may "
            "modify the art. Short, plain licence -- no revenue cap, no device cap."
        ),
        "redistributable": "no",
        "public_repo_safe": False,
        "attribution_required": False,
        "attribution_note": "Credit not required but explicitly appreciated. Worth giving.",
        "licence_evidence": "License.txt in the pack, quoted in full: personal + commercial OK, modification OK, no redistribution or resale",
        "theme": "2D warrior character, sprite sheet, animated, side-scroller, pixel",
        "note": (
            "Obtained via the Asset Store as 'Warrior Free Asset', but the pack "
            "carries its own licence file rather than relying on the store EULA."
        ),
    },
    "SEMA Game Studio": {
        **UNITY_FREE_COMMON,
        "author": "SEMA Game Studio",
        "theme": "cute cat character, animated, single model",
    },
    "Cute Birds": {
        **UNITY_FREE_COMMON,
        "author": "unknown -- check Asset Store purchase history",
        "theme": "2D birds, chicken, sprites, animated, PSD sources",
        "engine": "any",
        "engine_note": "2D PNG/PSD sprites plus Unity animation controllers. Sprites port anywhere; controllers do not.",
    },
    "Free Stylized Skybox": {
        **UNITY_FREE_COMMON,
        "author": "unknown -- check Asset Store purchase history",
        "theme": "skyboxes, sky, cubemap, panoramic, stylized",
        "engine": "any",
        "engine_note": "Cubemap and panoramic PNGs. The images port anywhere; the Unity .mat files do not.",
    },
}

ROOTS = assetpaths.roots()

# ---------------------------------------------------------------------------
# C:\Projects\itch_assets. Unlike the two roots above these packs sit at mixed
# depths and come from many vendors, so each is listed explicitly with its own
# verified licence. `extra_paths` folds in a second directory holding the same
# content in another format.
# ---------------------------------------------------------------------------

ITCH = assetpaths.HOME

QUATERNIUS_CC0 = {
    "licence": "CC0-1.0",
    "licence_name": "Creative Commons Zero 1.0 (public domain dedication)",
    "author": "Quaternius",
    "source": "https://quaternius.com",
    "engine": "any",
    "engine_note": "Plain FBX/GLTF/OBJ/Blend meshes. Import anywhere.",
    "transferable": "yes",
    "transferable_note": "No restriction. Use, modify, sublicense, sell, in any engine.",
    "redistributable": "yes",
    "public_repo_safe": True,
    "attribution_required": False,
    "attribution_note": "Not required. Quaternius runs on Patreon support; credit is kind.",
    "licence_evidence": "License.txt / License_Standard.txt in the pack states CC0 1.0",
}

QUATERNIUS_QAL = {
    **QUATERNIUS_CC0,
    "licence": "Quaternius Asset License (QAL) v1.0",
    "licence_name": "Quaternius Asset License v1.0 (https://quaternius.com/license.html)",
    "transferable": "yes-with-conditions",
    "transferable_note": (
        "Any engine, any project, commercial included, no credit required. The only "
        "bar is reselling: you may not repackage the assets as a standalone asset "
        "pack, stock file or template, however heavily modified."
    ),
    "redistributable": "no",
    "public_repo_safe": False,
    "attribution_required": False,
    "licence_evidence": (
        "License_Standard.txt states Quaternius Asset License (QAL) v1.0. Note "
        "Quaternius moved from CC0 to QAL for newer packs -- never assume, read "
        "the file that ships with each pack."
    ),
}

SONNISS = {
    "licence": "Sonniss #GameAudioGDC Bundle EULA (royalty-free)",
    "licence_name": "Sonniss GDC Game Audio Bundle Licensing Agreement",
    "author": "Sonniss LTD and ~70 contributing sound libraries",
    "source": "https://sonniss.com",
    "engine": "any",
    "engine_note": "Plain WAV. No engine involvement at all.",
    "transferable": "yes-with-conditions",
    "transferable_note": (
        "Worldwide, royalty-free, perpetual, unlimited projects, personal and "
        "commercial, no attribution, modification allowed. Ship inside your game."
    ),
    "redistributable": "no",
    "public_repo_safe": False,
    "attribution_required": False,
    "attribution_note": "Not required.",
    "licence_evidence": "License.pdf shipped in each bundle directory",
    "extra_restrictions": [
        "May not sell the sound effects as they come (only as incorporated into a product).",
        "May not modify a sound with intent to claim authorship of the original recording.",
        "NO AI TRAINING -- expressly prohibited from training any audio-generating model.",
        "Governed by English law, exclusive jurisdiction of the English courts.",
    ],
}

ALKAKRAB = {
    "licence": "AlkaKrab Music License (royalty-free)",
    "licence_name": "AlkaKrab Music License Agreement",
    "author": "AlkaKrab",
    "source": "alkakrab04@gmail.com",
    "engine": "any",
    "engine_note": "Plain WAV/MP3.",
    "transferable": "yes-with-conditions",
    "transferable_note": (
        "Royalty-free, commercial use allowed regardless of revenue, unlimited "
        "projects by the same purchaser. Timing/volume/EQ edits allowed."
    ),
    "redistributable": "no",
    "public_repo_safe": False,
    "attribution_required": False,
    "attribution_note": "Credit appreciated, not required.",
    "licence_evidence": "AlkaKrab Music License Info.pdf in game_sounds/alkaKrab",
    "extra_restrictions": [
        "OPEN SOURCE NEEDS WRITTEN PERMISSION -- the licence names this case "
        "explicitly: contact AlkaKrab before using these in an open-source game.",
        "No remixing or sampling without written permission (plain edits are fine).",
        "No uploading the tracks as-is to Spotify/Apple Music/etc.",
        "No redistribution or resale of the files as-is.",
    ],
}

CHEQUERED_INK = {
    "licence": "Chequered Ink All Fonts Pack licence (paid, small-business tier)",
    "licence_name": "Chequered Ink Ltd. Font Software License Agreement - All Fonts Pack (2024-12-01)",
    "author": "Chequered Ink Ltd. (England & Wales, company no. 09646754)",
    "source": "http://chequered.ink",
    "engine": "any",
    "engine_note": "TTF/OTF font files. Usable in any engine or design tool.",
    "transferable": "no",
    "transferable_note": (
        "Explicitly non-assignable and non-transferable, and capped at 5 Licensed "
        "Units (devices you own). Personal AND commercial use are both granted."
    ),
    "redistributable": "no",
    "public_repo_safe": False,
    "attribution_required": False,
    "attribution_note": "Not required. Keep proof of purchase -- the agreement says holding the licence text is NOT proof of eligibility.",
    "licence_evidence": "License Agreement - All Fonts Pack.pdf, read in full",
    "extra_restrictions": [
        "ELIGIBILITY IS CONDITIONAL: valid only while you are an individual or an "
        "org with 20 or fewer employees AND turnover of $2,000,000 USD or less. "
        "Outgrow either and the licence stops covering you.",
        "Max 5 Licensed Units (devices). No installing on a server unless every "
        "device that can reach it is a Licensed Unit.",
        "In a game the font must be protected from extraction by the end user, and "
        "the game must not let players set their own text in the font.",
        "NO AI TRAINING -- liquidated damages of $10,000 USD per infringement, and "
        "the clause says ignorance of a font's origin is not a defence.",
        "No lending, renting, sublicensing, selling, disassembling or reverse-engineering.",
    ],
}

SUNGRAPHICA = {
    "licence": "UNKNOWN -- no licence file present",
    "licence_name": "Not determined; assets credited to SunGraphica via gamedevmarket.net",
    "author": "SunGraphica",
    "source": "https://www.gamedevmarket.net/member/sgasset",
    "engine": "any",
    "engine_note": "PNG/PSD/AI/EPS/SVG 2D UI art.",
    "transferable": "unknown",
    "transferable_note": (
        "TREAT AS RESTRICTED UNTIL VERIFIED. No licence file ships with these "
        "folders -- only a contact note. GameDev Market sells under Pro and "
        "non-Pro tiers with materially different terms, so which one applies "
        "depends on your purchase. Check your GameDev Market receipt."
    ),
    "redistributable": "no",
    "public_repo_safe": False,
    "attribution_required": False,
    "attribution_note": "Unknown -- verify against your purchase.",
    "licence_evidence": (
        "NONE on disk. Only 'Sungraphica + info .txt' naming the vendor. "
        "ACTION: confirm the tier from your gamedevmarket.net purchase history."
    ),
}

UNKNOWN_LICENCE = {
    "licence": "UNKNOWN -- no licence file present",
    "licence_name": "Not determined",
    "author": "unknown",
    "source": "unknown",
    "engine": "any",
    "engine_note": "",
    "transferable": "unknown",
    "transferable_note": "TREAT AS RESTRICTED UNTIL VERIFIED.",
    "redistributable": "no",
    "public_repo_safe": False,
    "attribution_required": False,
    "attribution_note": "Unknown.",
    "licence_evidence": "NONE on disk.",
}


def itch(name: str, rel: str, meta: dict, **kw) -> dict:
    """One catalog entry rooted under C:\\Projects\\itch_assets."""
    spec = {"name": name, "path": ITCH / rel, "meta": meta, "root_kind": "itch"}
    spec.update(kw)
    return spec


EXPLICIT_PACKS: list[dict] = [
    # --- Quaternius: CC0 for the older packs, QAL v1.0 for the newer ones ----
    itch("Quaternius Bestiary - Dungeon Monsters Kit", "quaternius/Bestiary - Dungeon Monsters Kit[Standard]",
         {**QUATERNIUS_QAL, "theme": "monsters, bestiary, dungeon, creatures, enemies",
          "note": "FREE 'Standard' tier -- a subset of the models. The full 7-monster SOURCE version is a paid upgrade."}),
    itch("Quaternius Fantasy Props MegaKit", "quaternius/Fantasy Props MegaKit[Standard]",
         {**QUATERNIUS_CC0, "theme": "fantasy props, furniture, containers, dungeon dressing"}),
    itch("Quaternius Modular Character Outfits - Fantasy", "quaternius/Modular Character Outfits - Fantasy[Standard]",
         {**QUATERNIUS_CC0, "theme": "modular characters, outfits, fantasy, armour, clothing"}),
    itch("Quaternius Nature Kit", "quaternius/Nature Kit",
         {**QUATERNIUS_CC0, "theme": "nature, trees, rocks, platformer, outdoors"}),
    itch("Quaternius Stylized Nature MegaKit", "quaternius/Stylized Nature MegaKit[Standard]",
         {**QUATERNIUS_CC0, "theme": "stylized nature, trees, foliage, terrain, biomes"}),
    itch("Quaternius Universal Animation Library", "quaternius/Universal Animation Library[Standard]",
         {**QUATERNIUS_CC0, "theme": "character animations, locomotion, humanoid, retargetable"}),
    itch("Quaternius Universal Animation Library 2", "quaternius/Universal Animation Library 2[Standard]",
         {**QUATERNIUS_CC0, "theme": "character animations, locomotion, humanoid, retargetable"}),
    itch("Quaternius Dungeon Kit", "quaternius/dungeon kit",
         {**QUATERNIUS_CC0, "theme": "dungeon, modular interiors, walls, floors, props"}),
    # --- Audio ---------------------------------------------------------------
    itch("Sonniss GDC 2023 Game Audio Bundle", "game_sounds",
         {**SONNISS, "theme": "sound effects, foley, ambience, impacts, SFX library",
          "note": "14 bundle parts. Contributors are ~45 separate pro libraries; the "
                  "per-bundle Readme.txt credits them and points at their full products."},
         subdir_glob="Sonniss.com-GDC2023-*"),
    itch("Sonniss GDC 2024 Game Audio Bundle", "game_sounds",
         {**SONNISS, "theme": "sound effects, foley, ambience, impacts, SFX library",
          "note": "9 bundle parts. Contributors are ~30 separate pro libraries."},
         subdir_glob="Sonniss.com-GDC2024-*"),
    itch("AlkaKrab game music", "game_sounds/alkaKrab",
         {**ALKAKRAB, "theme": "music, ambient, dark ambient, action, loops, background score",
          "note": "28 tracks as WAV here; the same 28 titles are mirrored as MP3 in "
                  "itch_assets/mp3, which this entry also indexes."},
         extra_paths=[ITCH / "mp3"]),
    # --- Fonts ---------------------------------------------------------------
    itch("Chequered Ink All Fonts Pack (Sorted)", "Chequered-Ink-All-Fonts-Pack-Sorted",
         {**CHEQUERED_INK, "theme": "fonts, typefaces, display, pixel, sci-fi, horror, handwriting",
          "note": "Sorted into 20 style categories: Blackletter, Brushstroke, Cartoon, "
                  "Computer, Grunge, Handwriting, Horror, Outline, Picture-Like, Pixel, "
                  "Sans Heavy, Sans Light, Sci-Fi, Serif and more."}),
    # --- 2D UI art -----------------------------------------------------------
    itch("SunGraphica Fantasy UI collection", "fantasy",
         {**SUNGRAPHICA, "theme": "fantasy UI, icons, menus, dialogue borders, RPG interface"}),
    itch("SunGraphica Sci-Fi UI collection", "scifi",
         {**SUNGRAPHICA, "theme": "sci-fi UI, HUD, minimal icons, futuristic interface"}),
    # --- Unclassified --------------------------------------------------------
    itch("Goblin Chess All Assets", "Goblin Chess All Assets",
         {**UNKNOWN_LICENCE, "theme": "chess game UI, screens, cards, menus, Figma source",
          "author": "unknown -- possibly own/commissioned work",
          "note": "Ships a goblins.fig Figma source alongside exported screens, which "
                  "suggests bespoke work rather than a stock pack. CONFIRM ORIGIN: if "
                  "you commissioned it, check the contract for what rights transferred."}),
]


def classify(ext: str) -> str | None:
    if ext in MODEL_EXT:
        return "model"
    if ext in TEXTURE_EXT:
        return "texture"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in FONT_EXT:
        return "font"
    return None


def base_name(stem: str) -> str:
    """Strip the TOON Series colour-scheme suffix so variants collapse.

    SICS names assets `TFP_Barn_01A`, `TFP_Barn_01B`, ... where the trailing
    capital letter is only a recolour. Someone searching for a barn wants one
    hit, not five, so fold `_01A` -> `_01`. Names not in that shape are left
    alone -- Kenney uses a completely different convention.
    """
    if "_" not in stem:
        return stem
    head, _, tail = stem.rpartition("_")
    if len(tail) >= 2 and tail[:-1].isdigit() and tail[-1].isupper():
        return f"{head}_{tail[:-1]}"
    return stem


def scan_pack(pack_dirs: list[Path]) -> dict:
    """Index one pack, one record per real asset file.

    Takes a list of directories because some packs are split across several --
    a Sonniss bundle is 14 sibling folders, and AlkaKrab ships the same tracks
    twice in different formats.

    The pack's "primary kind" is whichever of model/audio/font/texture has the
    most files. That is what gets indexed by name, so a font pack indexes fonts
    and a model pack indexes meshes without needing to be told which it is.
    """
    counts: dict[str, int] = {}
    ext_counts: dict[str, int] = {}
    by_kind: dict[str, list[str]] = {}
    all_paths: list[str] = []
    colliders = 0

    for pack_dir in pack_dirs:
        for path in pack_dir.rglob("*"):
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            if ext in SKIP_EXT:
                continue
            # Kept regardless of kind: the UI/motion/modular labeller reads
            # .anim and .controller files that no other part of the catalog
            # indexes, and it needs folder names to judge context.
            all_paths.append(path.relative_to(pack_dir).as_posix())
            kind = classify(ext)
            if kind is None:
                continue
            counts[kind] = counts.get(kind, 0) + 1
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            if kind == "model" and "meshcollider" in path.stem.lower():
                # Collision hulls for a model listed separately; counting them
                # as models roughly doubles some packs for no reason.
                colliders += 1
                continue
            by_kind.setdefault(kind, []).append(
                path.relative_to(pack_dir).as_posix()
            )

    # Models win outright whenever present: a 3D pack always ships more textures
    # than meshes (albedo/normal/roughness per model), so counting would index
    # the wrong thing. Nothing else has that parasitic relationship, so among
    # the rest most files wins -- which keeps a GUI pack that happens to bundle
    # five fonts indexed as images rather than as a font pack.
    if by_kind.get("model"):
        primary = "model"
    else:
        rest = {k: v for k, v in by_kind.items() if k in ("font", "audio", "texture")}
        primary = max(rest, key=lambda k: len(rest[k])) if rest else "none"
    files = sorted(by_kind.get(primary, []))

    # De-duplicate by stem: Kenney ships the same mesh as glb/fbx/obj/dae and
    # the UI packs ship the same icon at 128/256/512/1024px. Listing each four
    # times helps nobody searching.
    stems = sorted({Path(m).stem for m in files})
    bases = sorted({base_name(s) for s in stems})

    return {
        "counts": counts,
        "extensions": dict(sorted(ext_counts.items(), key=lambda kv: -kv[1])),
        "collider_mesh_count": colliders,
        "primary_kind": primary,
        "model_files": files,
        "model_names": stems,
        "distinct_objects": bases,
        "unique_model_count": len(stems),
        "distinct_object_count": len(bases),
        "indexed_file_count": len(all_paths),
        # Every file, not just the primary kind -- the search index needs the
        # textures and animation clips that the kind-based index leaves out.
        "all_files": all_paths,
    }


def split_store_category(cat: str) -> str:
    """'3D ModelsEnvironmentsFantasy' -> '3D Models / Environments / Fantasy'.

    Unity flattens the store's category tree into one directory name with no
    separator. Splitting on the capital that starts each word recovers it well
    enough to read and to search, which is all the catalog needs.
    """
    if not cat:
        return "uncategorised"
    out, buf = [], ""
    for i, ch in enumerate(cat):
        if ch.isupper() and buf and not buf[-1].isspace() and not cat[i - 1].isupper():
            out.append(buf)
            buf = ch
        else:
            buf += ch
    out.append(buf)
    return " / ".join(s.strip() for s in out if s.strip())


def load_unity_cache() -> list[dict]:
    """Fold the Asset Store download cache into the catalog, if it was indexed.

    These packages are licensed and on disk but not imported into any project,
    so they are worth knowing about when sourcing art -- with `imported` false
    so nobody goes looking for the files under Assets/.
    """
    cache_file = OUT_DIR / "unity-cache-catalog.json"
    if not cache_file.exists():
        print("\n  (no unity-cache-catalog.json -- run build_unity_cache_index.py)")
        return []

    data = json.loads(cache_file.read_text(encoding="utf-8"))
    packs = []
    for r in data["packages"]:
        category = split_store_category(r["store_category"])
        packs.append(
            {
                "pack": r["package"],
                "path": r["file"],
                "root_kind": "unity-cache",
                "imported": False,
                **UNITY_FREE_COMMON,
                "author": r["publisher"],
                "source": "https://assetstore.unity.com",
                "theme": category.lower().replace(" / ", ", "),
                "store_category": category,
                "size_mb": r["size_mb"],
                "downloaded": r["downloaded"],
                "note": (
                    f"DOWNLOADED BUT NOT IMPORTED -- {r['size_mb']} MB "
                    f".unitypackage sitting in the Asset Store cache. Import it "
                    f"through the Package Manager's My Assets tab to use it."
                ),
                "licence_evidence": (
                    "Present in the Unity Asset Store download cache, so it is "
                    "licensed to this account under the Asset Store EULA. Individual "
                    "packs sometimes ship their own licence file inside -- check "
                    "after importing."
                ),
                "counts": r["counts"],
                "extensions": {},
                "collider_mesh_count": 0,
                "primary_kind": r["primary_kind"],
                "model_files": r.get("asset_paths", []),
                "model_names": r["distinct_objects"],
                "distinct_objects": r["distinct_objects"],
                "unique_model_count": r["distinct_object_count"],
                "distinct_object_count": r["distinct_object_count"],
                "indexed_file_count": r.get("asset_count", 0),
                # Labelled from the full manifest, which includes the .anim and
                # .controller files the kind-based index skips.
                **label_pack(
                    r["package"], r["store_category"], r.get("asset_paths", [])
                ),
            }
        )
    print(f"\n  merged {len(packs)} cached Asset Store packages")
    return packs


def main() -> None:
    packs_out = []
    unknown = []

    for root, root_kind in ROOTS:
        if not root.is_dir():
            print(f"  ! missing root: {root}")
            continue
        for pack_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            name = pack_dir.name
            meta = PACKS.get(name)
            if meta is None:
                # Not a licensed pack we track (Unity project folders, etc.).
                unknown.append(str(pack_dir))
                continue
            scanned = scan_pack([pack_dir])
            packs_out.append(
                {
                    "pack": name,
                    "path": str(pack_dir),
                    "root_kind": root_kind,
                    **meta,
                    **scanned,
                }
            )
            print(f"  {name}: {scanned['unique_model_count']} unique models")

    for spec in EXPLICIT_PACKS:
        base = spec["path"]
        glob = spec.get("subdir_glob")
        dirs = sorted(base.glob(glob)) if glob else [base]
        dirs = [d for d in dirs if d.is_dir()] + [
            p for p in spec.get("extra_paths", []) if p.is_dir()
        ]
        if not dirs:
            print(f"  ! missing: {spec['name']} at {base}")
            continue
        scanned = scan_pack(dirs)
        packs_out.append(
            {
                "pack": spec["name"],
                "path": str(base) + (f"  [{glob}]" if glob else ""),
                "indexed_directories": [str(d) for d in dirs],
                "root_kind": spec["root_kind"],
                **spec["meta"],
                **scanned,
            }
        )
        print(
            f"  {spec['name']}: {scanned['distinct_object_count']} distinct "
            f"{scanned['primary_kind']}s"
        )

    for p in packs_out:
        p["imported"] = True
        p.update(
            label_pack(p["pack"], p.get("theme", ""), p.get("all_files", []))
        )
    packs_out += load_unity_cache()

    catalog = {
        "generated": date.today().isoformat(),
        "generator": "build_asset_catalog.py",
        "disclaimer": (
            "Licence summaries are a good-faith reading of the shipped licence "
            "files and published store terms, not legal advice. Confirm before "
            "a commercial release."
        ),
        "licence_classes": {
            "CC0-1.0": {
                "redistributable": True,
                "public_repo_safe": True,
                "engine_locked": False,
                "summary": "Public domain. Do anything, including reselling the raw assets.",
            },
            "Unity Asset Store EULA (Restricted Assets)": {
                "redistributable": False,
                "public_repo_safe": False,
                "engine_locked": False,
                "summary": (
                    "Ship inside a built product in any engine. Never redistribute "
                    "the source assets -- no reselling, no asset packs, no public "
                    "repo, no NFT use, no AI training data, no per-client transfer."
                ),
            },
            "Quaternius Asset License (QAL) v1.0": {
                "redistributable": False,
                "public_repo_safe": False,
                "engine_locked": False,
                "summary": (
                    "Any engine, any project, commercial included, no credit owed. "
                    "Only bar is repackaging the assets as a standalone asset pack."
                ),
            },
            "Sonniss #GameAudioGDC Bundle EULA (royalty-free)": {
                "redistributable": False,
                "public_repo_safe": False,
                "engine_locked": False,
                "summary": (
                    "Unlimited projects, commercial, no attribution, perpetual. "
                    "Cannot sell the sounds as-is. No AI training. English law."
                ),
            },
            "AlkaKrab Music License (royalty-free)": {
                "redistributable": False,
                "public_repo_safe": False,
                "engine_locked": False,
                "summary": (
                    "Commercial use at any revenue, unlimited projects. Open-source "
                    "use needs the author's written permission. No remix/sampling."
                ),
            },
            "Chequered Ink All Fonts Pack licence (paid, small-business tier)": {
                "redistributable": False,
                "public_repo_safe": False,
                "engine_locked": False,
                "summary": (
                    "Paid, personal and commercial, but non-transferable, capped at "
                    "5 devices, conditional on staying under 20 staff and $2M "
                    "turnover, fonts must be extraction-protected in a shipped game, "
                    "and AI training carries a $10k-per-infringement clause."
                ),
            },
            "Clembod free-asset licence": {
                "redistributable": False,
                "public_repo_safe": False,
                "engine_locked": False,
                "summary": (
                    "Personal and commercial use granted outright, modification "
                    "allowed, credit appreciated. No redistribution or resale."
                ),
            },
            "UNKNOWN -- no licence file present": {
                "redistributable": False,
                "public_repo_safe": False,
                "engine_locked": False,
                "summary": (
                    "Provenance not established on disk. Treat as fully restricted "
                    "until the purchase receipt or contract is checked."
                ),
            },
        },
        "pack_count": len(packs_out),
        "imported_pack_count": sum(1 for p in packs_out if p.get("imported")),
        "cached_pack_count": sum(1 for p in packs_out if not p.get("imported")),
        "unity_cache_path": str(assetpaths.UNITY_CACHE),
        "packs": packs_out,
        "untracked_directories": unknown,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "asset-catalog.json"
    json_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    print(f"\nwrote {json_path}")

    write_markdown(catalog)


def write_markdown(catalog: dict) -> None:
    lines: list[str] = []
    a = lines.append

    a("# Owned Game Asset Catalog")
    a("")
    a(f"Generated {catalog['generated']} by `build_asset_catalog.py`. Re-run that")
    a("script after adding a pack; add the pack's licence row to `PACKS` first.")
    a("")
    a(f"> {catalog['disclaimer']}")
    a("")
    a("Machine-readable index with every model filename: [asset-catalog.json](asset-catalog.json)")
    a("")

    on_disk = [p for p in catalog["packs"] if p.get("imported")]
    cached = [p for p in catalog["packs"] if not p.get("imported")]

    a("## Quick answer: what may I do with each pack?")
    a("")
    a(f"These {len(on_disk)} packs are extracted and usable right now.")
    a("")
    a("| Pack | Licence | Engine | Transferable | Redistribute source | Public repo | Contains |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    for p in on_disk:
        a(
            f"| {p['pack']} | {p['licence']} | {p['engine']} | "
            f"{p['transferable']} | {p['redistributable']} | "
            f"{'yes' if p['public_repo_safe'] else 'NO'} | "
            f"{', '.join(p.get('traits', [])) or '—'} |"
        )
    a("")

    a("## Cross-cutting labels: UI, motion, modular")
    a("")
    a("Every file in every pack is checked against the UI / motion / modular")
    a("rules in `asset_labels.py`. A pack earns a trait when matches reach 10% of")
    a("its files or 25 files outright, so a stray `Wall_01` does not make a prop")
    a("pack modular. Per-asset lists live in `asset-catalog.json` under")
    a("`ui_assets`, `motion_assets` and `modular_assets`.")
    a("")
    for label, blurb in (
        ("ui", "icons, HUD, menus, buttons, panels, cursors"),
        ("motion", "animation clips, controllers, rigged/`Model@Clip` meshes"),
        ("modular", "snap-together kit pieces: corners, straights, walls, tiles"),
    ):
        hits = sorted(
            (p for p in catalog["packs"] if label in p.get("traits", [])),
            key=lambda x: -x[f"{label}_count"],
        )
        total = sum(p[f"{label}_count"] for p in catalog["packs"])
        a(f"### {label} — {blurb}")
        a("")
        a(f"{total} labelled assets across the whole library; "
          f"{len(hits)} packs carry the trait.")
        a("")
        a("| Pack | Status | Labelled | Evidence |")
        a("| --- | --- | --- | --- |")
        for p in hits:
            a(
                f"| {p['pack']} | {'imported' if p.get('imported') else 'cached'} | "
                f"{p[f'{label}_count']} | {p.get('trait_evidence', {}).get(label, '')} |"
            )
        a("")

    if cached:
        size = sum(p.get("size_mb", 0) for p in cached) / 1024
        objs = sum(p["distinct_object_count"] for p in cached)
        a("## Owned but NOT imported — the Unity Asset Store download cache")
        a("")
        a(f"{len(cached)} packages, {size:.1f} GB, {objs} distinct assets, sitting in")
        a(f"`{catalog['unity_cache_path']}`.")
        a("")
        a("Every one is licensed to this Unity account and already downloaded, but")
        a("none is in a project. Import through Package Manager > My Assets. All are")
        a("Unity Asset Store EULA: any engine, ship-in-built-product only, never")
        a("redistribute. Some ship their own licence file inside — check on import.")
        a("")
        a("| Package | Publisher | Store category | Size | Assets | Kind | Contains |")
        a("| --- | --- | --- | --- | --- | --- | --- |")
        for p in sorted(cached, key=lambda x: (x["author"].lower(), x["pack"].lower())):
            a(
                f"| {p['pack']} | {p['author']} | {p.get('store_category', '')} | "
                f"{p.get('size_mb', 0):.0f} MB | {p['distinct_object_count']} | "
                f"{p['primary_kind']} | {', '.join(p.get('traits', [])) or '—'} |"
            )
        a("")

    a("## Contents by pack")
    a("")
    a("Imported packs only. For the cached packages see the table above, and")
    a("`unity-cache-catalog.json` for their full per-file manifests.")
    a("")
    for p in on_disk:
        a(f"### {p['pack']}")
        a("")
        a(f"- **Path:** `{p['path']}`")
        a(f"- **Author:** {p['author']} — {p['source']}")
        a(f"- **Theme:** {p['theme']}")
        a(f"- **Licence:** {p['licence_name']}")
        a(f"- **Licence evidence:** {p['licence_evidence']}")
        a(f"- **Engine:** {p['engine']} — {p['engine_note']}")
        a(f"- **Transferable:** {p['transferable']} — {p['transferable_note']}")
        a(f"- **Attribution:** {'required' if p['attribution_required'] else 'not required'} — {p['attribution_note']}")
        if p.get("naming_prefix"):
            a(f"- **Filename prefix:** `{p['naming_prefix']}_`")
        if p.get("note"):
            a(f"- **Note:** {p['note']}")
        if p.get("extra_restrictions"):
            a("- **Watch out for:**")
            for r in p["extra_restrictions"]:
                a(f"    - {r}")
        counts = ", ".join(f"{v} {k}" for k, v in sorted(p["counts"].items()))
        a(f"- **Files:** {counts or 'none indexed'}")
        a(f"- **Indexed as:** {p['primary_kind']} -- {p['unique_model_count']} files, {p['distinct_object_count']} distinct after folding variants")
        if p.get("traits"):
            bits = []
            for t in p["traits"]:
                bits.append(f"**{t}** ({p[f'{t}_count']} assets)")
            a(f"- **Labelled contents:** {', '.join(bits)}")
        for t in ("ui", "motion", "modular"):
            n = p.get(f"{t}_count", 0)
            if n and t not in p.get("traits", []):
                a(f"- **Some {t} content:** {n} assets, below the trait threshold")
        a("")

    md_path = OUT_DIR / "ASSET-CATALOG.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
