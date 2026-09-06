"""Classify individual assets as UI, motion, or modular.

The catalog can already answer "which pack has a barn". It could not answer
"where are my UI icons", "which packs ship animation clips", or "which kits are
modular" -- those cut across packs and across the model/texture/audio split,
so they need their own labels.

Labels are heuristic. Every one is derived from the file path and recorded with
the evidence that triggered it, so a wrong call is visible rather than silent.
Shared by build_asset_catalog.py and build_unity_cache_index.py so the imported
packs and the download cache are labeled by identical rules.

The rules are deliberately conservative. An earlier, looser version labelled
`cliff_blockCave_rock` as motion (it contains "block", an attack verb),
`ceilingFan` as modular (its pack is called a "kit", and "ceiling" is a
connector word), and Harley Davidson engine recordings as modular ("turn" plus
"start"). Each of those produced a lesson encoded below: match whole tokens not
substrings, never let a pack name alone promote a file, and keep ambiguous
nouns out of the verb lists.
"""

from __future__ import annotations

import re
from pathlib import Path

# Split CamelCase, snake_case, kebab-case and paths into lowercase tokens.
# Tokenising matters: substring matching would label every "Trunk" as a "run"
# animation and every "Building" as a "build" tool.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

AUDIO_EXT = {".wav", ".ogg", ".mp3", ".flac", ".aiff"}
TEXTURE_EXT = {".png", ".jpg", ".jpeg", ".psd", ".tga", ".exr", ".tif", ".tiff", ".svg", ".ai", ".eps"}


def tokens(text: str) -> set[str]:
    spaced = _CAMEL.sub(" ", text)
    return {t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t}


# --- UI -------------------------------------------------------------------
UI_TOKENS = {
    "ui", "gui", "hud", "icon", "icons", "menu", "menus", "interface",
    "button", "buttons", "panel", "panels", "cursor", "crosshair",
    "healthbar", "minimap", "tooltip", "dialogue", "popup", "inventory",
    "hotbar", "scrollbar", "slider", "checkbox", "banner", "badge",
    "widget", "toolbar",
}
# Directory names that make a whole subtree UI regardless of file naming.
UI_DIR_TOKENS = {"ui", "gui", "hud", "icons", "icon", "interface", "menu", "menus"}

# --- Motion ---------------------------------------------------------------
MOTION_EXT = {".anim", ".controller", ".overridecontroller", ".playable", ".mask"}
MOTION_DIR_TOKENS = {
    "animation", "animations", "anim", "anims", "motion", "motions",
    "clips", "mecanim",
}
# Words that mean "animation" almost wherever they appear in a filename.
# Ambiguous action nouns (block, fire, hit, cast, land, fall, wave, turn, roll,
# push, pull, aim, spawn, stand, sit) are deliberately absent -- they name props
# far more often than clips, and they were the source of every motion false
# positive in the first pass.
MOTION_TOKENS = {
    "idle", "walking", "running", "sprint", "jumping", "crouching",
    "attacking", "dying", "locomotion", "mecanim", "armature", "rigged",
    "animation", "animated", "animations", "taunt", "strafe", "dodge",
    "reload", "unarmed", "twohanded", "onehanded",
}
# A file whose whole name is one of these is a clip, even though the same word
# inside a longer name would be ambiguous. `Run.fbx` is a clip; `Trunk_01` and
# `cliff_blockCave_rock` are not.
CLIP_STEMS = {
    "idle", "walk", "run", "jump", "attack", "death", "die", "hit", "hurt",
    "dance", "crouch", "climb", "swim", "fly", "sit", "stand", "turn",
    "roll", "dodge", "block", "cast", "reload", "aim", "shoot", "fire",
    "punch", "kick", "throw", "land", "fall", "push", "pull", "victory",
    "defeat", "wave", "sleep", "eat", "drink", "spawn", "taunt", "strafe",
}

# --- Modular --------------------------------------------------------------
# Only "modular" and "tileset" are explicit enough to promote on their own.
# "kit" is NOT here: half the packs in this library are called a kit, and
# treating that as evidence made every ceiling fan a modular building piece.
MODULAR_TOKENS = {"modular", "tileset"}
# Connector-shaped names: the giveaway that a mesh is meant to snap to others.
MODULAR_PART_TOKENS = {
    "corner", "straight", "end", "junction", "cross", "tjunction", "middle",
    "edge", "side", "center", "centre", "ramp", "stair", "stairs", "slope",
    "wall", "floor", "ceiling", "roof", "doorway", "window", "arch",
    "pillar", "column", "beam", "railing", "fence", "path", "road",
    "segment", "piece", "connector", "cap", "curve", "half", "tile", "tiles",
}


def _path_tokens(path: str) -> tuple[set[str], set[str], set[str]]:
    """Return (all tokens, directory tokens, filename tokens) for a path."""
    p = Path(path)
    dir_tokens: set[str] = set()
    for part in p.parts[:-1]:
        dir_tokens |= tokens(part)
    name_tokens = tokens(p.stem)
    return dir_tokens | name_tokens, dir_tokens, name_tokens


def _clip_stem(stem: str) -> str:
    """Strip trailing numbering and separators: 'Run_01' -> 'run'."""
    return re.sub(r"[^a-z]", "", re.sub(r"[\s_\-]*\d+$", "", stem.strip().lower()))


def label_path(path: str, pack_tokens: set[str] | None = None) -> set[str]:
    """Return the set of labels ('ui', 'motion', 'modular') that fit this file."""
    pack_tokens = pack_tokens or set()
    p = Path(path)
    all_t, dir_t, name_t = _path_tokens(path)
    ext = p.suffix.lower()
    labels: set[str] = set()

    # Mesh colliders are invisible helper geometry; labelling them just doubles
    # every count for the packs that ship them.
    if "meshcollider" in p.stem.lower():
        return labels

    # UI: a UI directory, a UI word in the filename, or a UI-themed pack. Pack
    # context is allowed here because a pack called "Sci-Fi UI collection" is
    # genuinely UI end to end, including its sounds.
    if (dir_t & UI_DIR_TOKENS) or (name_t & UI_TOKENS) or (pack_tokens & UI_DIR_TOKENS):
        labels.add("ui")

    # Motion: an animation file type, an animation folder, Unity's
    # `Model@Clip.fbx` convention, an unambiguous animation word, or a filename
    # that is nothing but a clip name.
    if (
        ext in MOTION_EXT
        or (dir_t & MOTION_DIR_TOKENS)
        or "@" in p.stem
        or (name_t & MOTION_TOKENS)
        or _clip_stem(p.stem) in CLIP_STEMS
    ):
        labels.add("motion")

    # Modular: never audio -- a recording of a roof tile is not a kit piece.
    # Otherwise: an explicit "modular"/"tileset" word plus a connector word, a
    # filename that just says "modular", or two distinct connector words in the
    # filename itself. Pack name alone is never enough.
    if ext not in AUDIO_EXT:
        parts_hit = name_t & MODULAR_PART_TOKENS
        explicit = (all_t | pack_tokens) & MODULAR_TOKENS
        if "modular" in name_t or "tileset" in name_t:
            labels.add("modular")
        elif explicit and parts_hit:
            labels.add("modular")
        elif len(parts_hit) >= 2:
            labels.add("modular")

    return labels


def label_pack(pack: str, category: str, paths: list[str]) -> dict:
    """Label every file in a pack and summarise what the pack contains.

    Returns per-label asset lists (deduplicated to stems, matching how the rest
    of the catalog indexes) plus the evidence behind each pack-level trait.
    """
    pack_tokens = tokens(pack) | tokens(category)

    buckets: dict[str, set[str]] = {"ui": set(), "motion": set(), "modular": set()}
    for path in paths:
        for label in label_path(path, pack_tokens):
            buckets[label].add(Path(path).stem)

    total = len(paths) or 1
    traits: list[str] = []
    evidence: dict[str, str] = {}
    for label, names in buckets.items():
        if not names:
            continue
        share = len(names) / total
        # A handful of matches in a thousand-file pack is noise, not a trait.
        # Either a real share of the pack, or an unambiguous count, qualifies.
        if share >= 0.10 or len(names) >= 25:
            traits.append(label)
            evidence[label] = (
                f"{len(names)} of {len(paths)} files ({share:.0%}) match the "
                f"{label} rules"
            )

    return {
        "traits": sorted(traits),
        "trait_evidence": evidence,
        "ui_assets": sorted(buckets["ui"]),
        "motion_assets": sorted(buckets["motion"]),
        "modular_assets": sorted(buckets["modular"]),
        "ui_count": len(buckets["ui"]),
        "motion_count": len(buckets["motion"]),
        "modular_count": len(buckets["modular"]),
    }
