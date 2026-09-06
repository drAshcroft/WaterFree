"""Derive human-readable context for an asset from its path.

A filename on its own is often meaningless. `Blaster_Albedo` is a texture map,
not a thing; `Asset 1 - 1024p` says nothing at all; `box.png` could be anything.
What gives them meaning is around them: the folder they sit in, the render
pipeline variant they came from, and the prefab or mesh they dress.

This module turns a path into three things:

  subject   what the asset actually depicts, with map/resolution/variant
            suffixes stripped -- `Blaster_Albedo` and `Blaster_Normal` share
            the subject `Blaster`, so they group instead of scrolling past.
  context   the meaningful folder trail, with boilerplate removed --
            `Assets/HiRez/Textures/Weapons/Blaster/x.png` becomes
            `Weapons > Blaster`.
  variant   which nested pipeline package it came from (BuiltIn vs URP), where
            the pack ships both.

Used by build_search_index.py so all three are searchable and displayable.
"""

from __future__ import annotations

import re
from pathlib import Path

# Folder names that appear in nearly every pack and describe file type rather
# than subject. The kind column already covers this, so they add no meaning.
FOLDER_STOPWORDS = {
    "assets", "asset", "prefabs", "prefab", "models", "model", "meshes", "mesh",
    "textures", "texture", "materials", "material", "fbx", "obj", "glb", "gltf",
    "png", "psd", "jpg", "tga", "source", "sources", "src", "art", "artwork",
    "exports", "export", "exported", "resources", "content", "files", "new",
    "previews", "preview", "thumbnails", "images", "img", "sprites", "sprite",
    "fbx format", "obj format", "gltf format", "dae format", "stl format",
    "png and psd", "for direct use", "standard", "lite", "free", "pack",
    "collection", "unity", "unitypackage", "builtin", "urp", "hdrp", "biRP",
    "low", "high", "med", "medium",
}

# Resolution / density folders: real, but never what you are searching for.
_RES_FOLDER = re.compile(r"^\d+\s*(px|p|k)$|^\d+x\d+$|^@\d+x$", re.I)

# Texture map channels. Stripping these is what collapses a material's five
# files into one subject.
MAP_SUFFIXES = {
    "albedo", "albedotransparency", "basecolor", "base", "color", "colour",
    "diffuse", "d", "normal", "normals", "n", "nrm", "metallic",
    "metallicsmoothness", "metallicness", "roughness", "rough", "smoothness",
    "specular", "spec", "gloss", "ao", "occlusion", "ambientocclusion",
    "emissive", "emission", "emi", "height", "displacement", "disp", "bump",
    "mask", "masks", "shadowmask", "opacity", "alpha", "transparency",
    "ramp", "curvature", "cavity", "detail", "mat", "tex",
}

# Orientation / variant tails that mark a sibling, not a different subject.
VARIANT_SUFFIXES = {
    "ne", "nw", "se", "sw", "n", "s", "e", "w", "copy", "nobg", "bg",
    "flip", "flipped", "mirror", "mirrored", "alt", "old", "new", "final",
    "l", "r", "left", "right", "front", "back", "top", "bottom",
}

_LOD = re.compile(r"^lod\d*$", re.I)
_RES_TOKEN = re.compile(r"^\d+\s*(px|p|k)$", re.I)
_NESTED = re.compile(r"^\[(?P<inner>[^\]]+)\]\s*")
# SICS ships colour schemes as a trailing capital on a numbered name:
# TFP_Barn_01A / _01B / _01C are recolours of one barn.
_COLOUR_VARIANT = re.compile(r"^(?P<base>.*_\d+)[A-Z]$")


def split_variant(path: str) -> tuple[str, str]:
    """Peel the `[URP_Foo.unitypackage] ` marker off a nested-package path."""
    m = _NESTED.match(path)
    if not m:
        return path, ""
    inner = m.group("inner")
    label = "URP" if inner.upper().startswith("URP") else (
        "Built-in" if re.match(r"(?i)^(builtin|birp)", inner) else inner
    )
    return path[m.end():], label


def subject_of(stem: str) -> str:
    """Reduce a filename to the thing it depicts.

    `Blaster_Albedo` -> `Blaster`, `Rock_02_LOD1` -> `Rock_02`,
    `TFP_Barn_01A` -> `TFP_Barn_01`, `Icon_128p` -> `Icon`.
    Never returns empty: if stripping removes everything, the original wins,
    because a bad grouping is worse than an ugly name.
    """
    parts = re.split(r"[_\s\-]+", stem.strip())
    while len(parts) > 1:
        tail = parts[-1].lower()
        # Map channels are often numbered -- Mask1, Mask2, Detail2. Strip the
        # digits before matching so those still collapse onto their subject.
        tail_base = re.sub(r"\d+$", "", tail)
        if (
            tail in MAP_SUFFIXES
            or (tail_base in MAP_SUFFIXES and tail_base != tail)
            or tail in VARIANT_SUFFIXES
            or _LOD.match(tail)
            or _RES_TOKEN.match(tail)
            or (tail.isdigit() and len(parts) > 2)
        ):
            parts.pop()
            continue
        break
    out = "_".join(parts) or stem
    m = _COLOUR_VARIANT.match(out)
    return m.group("base") if m else out


def context_of(path: str, pack: str) -> str:
    """The meaningful folder trail, boilerplate and pack-name echoes removed."""
    clean, _ = split_variant(path)
    segments = Path(clean).parts[:-1]
    pack_words = {w for w in re.split(r"[^a-z0-9]+", pack.lower()) if len(w) > 2}

    keep = []
    for seg in segments:
        low = seg.lower().strip()
        if not low or low in FOLDER_STOPWORDS or _RES_FOLDER.match(low):
            continue
        words = {w for w in re.split(r"[^a-z0-9]+", low) if len(w) > 2}
        # Drop folders that just restate the pack name.
        if words and words <= pack_words:
            continue
        keep.append(seg.strip())

    # Deduplicate consecutive repeats ("Toon City/Toon City/Props").
    trail = []
    for seg in keep:
        if not trail or trail[-1].lower() != seg.lower():
            trail.append(seg)
    return " > ".join(trail[-3:])


def describe(path: str, pack: str, kind: str) -> dict:
    """Full context record for one asset."""
    clean, variant = split_variant(path)
    p = Path(clean)
    subject = subject_of(p.stem)
    context = context_of(path, pack)

    # A name that carries no information on its own needs the folder to speak
    # for it -- `1.png` inside `Game Menu/Buttons` is a menu button.
    generic = bool(re.fullmatch(r"[\d\W_]+", p.stem)) or p.stem.lower() in {
        "asset", "untitled", "new", "default", "image", "texture", "sprite"
    }
    display = f"{context.split(' > ')[-1]} / {p.stem}" if generic and context else p.stem

    return {
        "subject": subject,
        "context": context,
        "variant": variant,
        "display": display,
        "generic_name": int(generic),
    }
