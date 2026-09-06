---
name: waterfree-assets
description: Search the user's owned game-asset library (3D models, textures, audio, fonts, animations, prefabs) by content, engine and licence before sourcing or buying art. Answers "do I already own a sci-fi gun I can use in Godot", "which fantasy UI buttons do I have", "is this asset safe to commit to a public repo". Use whenever a task needs art, audio, fonts or 3D models, or asks what assets are available, or whether a licence permits something.
---

# WaterFree — Owned Asset Library

The user owns a large, licensed game-asset library that is **not** discoverable
from any project's source tree. Most of it lives outside the repo, and a large
share is downloaded but not yet imported into any project.

**Before sourcing art, generating placeholder assets, or suggesting a purchase,
search this library.** The answer is very often "you already own that".

## Search

```bash
C:\Users\<you>\.claude\skills\waterfree-assets\assetsearch.cmd "<query>"
```

Or call the script directly with any Python 3.10+ (the tools are stdlib-only):

```bash
python <skill-dir>/bin/assetsearch.py "<query>" --subjects
```

Flags:

| Flag | Effect |
|---|---|
| `--subjects` | **Prefer this.** Groups files by the thing they depict, collapsing a material's five map files, LODs and resolution variants into one row. |
| `--packs` | Summarise by pack — the "which pack should I import" view. |
| `--limit N` | Rows to show (default 25). |
| `--json` | Machine-readable output. |

## Query language

Terms are comma- or space-separated, in any order. You do not label them; the
parser sorts them into facets and echoes back what it applied.

| Facet | Words |
|---|---|
| **Engine** | `unity` `godot` `unreal` `blender` `web` |
| **Rights** | `alterable` `commercial` `redistributable` `public-repo` `open-source` `cc0`/`free` `ai-training` |
| **Kind** | `model`/`3d`/`mesh`/`prop`, `texture`/`2d`/`sprite`, `audio`/`sound`/`sfx`/`music`, `font` |
| **Label** | `ui`/`gui`/`hud`/`icon`/`button`/`menu`, `motion`/`animation`/`clip`, `modular`/`tileset`, `source` (editable .blend/.psd/.fbx) |
| **Status** | `imported` (files on disk now) · `cached` (owned, still a `.unitypackage` — import first) |
| **Text** | anything else |

The engine facet is real, not cosmetic: `.prefab`, `.shader`, `.mat`, `.anim`
and `.cs` are Unity-only, `.fbx`/`.png`/`.wav` open anywhere, `.blend` needs an
export step outside Blender.

```bash
assetsearch.cmd "sci fi gun, godot, alterable" --subjects
assetsearch.cmd "fantasy, button, unity" --packs
assetsearch.cmd "barn" --subjects
assetsearch.cmd "walk idle animation, imported"
assetsearch.cmd "tree, cc0, godot, model" --packs
```

## Reading results

Filenames alone are usually meaningless (`Blaster_Albedo`, `1.png`), so each
row carries derived context:

- **subject** — map/LOD/resolution/colour-variant suffixes stripped, so
  `Blaster_Albedo` + `_Normal` + `_Mask1` group under `Blaster`
- **context** — folder trail with boilerplate removed: `Buildings > Barns`
- **prefab / mesh** — the prefab or mesh a texture dresses; a prefab name is
  usually the most human-readable label in a Unity pack
- **variant** — Built-in vs URP, for packs shipping both
- **flags** — `U` ui · `M` motion · `K` modular kit piece · `S` editable source
  · `[CACHED]` owned but needs importing first

Matching runs three precision tiers and stops at the first that returns
anything: the term must hit the asset's own name/subject/folder/prefab; then
pack name and theme are allowed (it says *"no direct name match"*); then any
term (it says so). Pack themes are marketing blurbs — Toon Farm Pack's theme
mentions "barn", which would otherwise make all 5,000 of its files match.

Typos and phrasings normalise (`sci fi` / `sci-fi` / `sci fy` → `scifi`, also
`low poly`, `top down`). Concept terms expand: `gun` also finds
rifle/pistol/blaster/shotgun/cannon; `tree` finds pine/oak/palm/foliage.

## Licences — read before shipping

The library spans several licences and they differ in ways that matter:

- **CC0** (Kenney, most Quaternius) — public domain, any engine, redistributable,
  safe in a public repo.
- **Unity Asset Store EULA** — any engine, but ship only inside a built product
  where users cannot extract the source. Never commit to a public repo.
- **Quaternius QAL v1.0** — like CC0 except you may not repackage as an asset
  pack. Quaternius switched mid-catalogue; read each pack's `License.txt`.
- **Sonniss audio** — royalty-free, unlimited projects, **no AI training**.
- **AlkaKrab music** — commercial fine, but **open-source use needs written
  permission** from the author.
- **Chequered Ink fonts** — paid, non-transferable, capped at 5 devices, and
  eligibility is conditional on staying under 20 staff / $2M turnover. Fonts
  must be extraction-protected in a shipped game. AI training carries a
  $10,000-per-infringement clause.
- **UNKNOWN** — two packs ship no licence file and are flagged
  `licence_unknown`; they deliberately fail the `commercial`/`alterable`
  filters until a receipt is checked.

Licence fields are a good-faith reading of shipped licence files and published
store terms, **not legal advice**. Flag this when a commercial release is at
stake.

## Configuration

Paths resolve through `bin/assetpaths.py`. To check what this machine is set to:

```bash
python <skill-dir>/bin/assetpaths.py
```

| Variable | Meaning | Default |
|---|---|---|
| `WATERFREE_ASSETS_HOME` | where catalogs and `asset-index.db` live | `C:\Projects\itch_assets` |
| `WATERFREE_UNITY_CACHE` | Unity Asset Store download cache | `%APPDATA%\Unity\Asset Store-5.x` |

Which directories are scanned is data, not code: put an `asset-sources.json`
in `WATERFREE_ASSETS_HOME` to override the built-in roots.

```json
{ "roots": [ { "path": "C:\\Projects\\kenny_assets", "kind": "kenney" } ] }
```

## Rebuilding the index

If a search finds nothing you expect, the index may be stale.

```bash
python bin/build_asset_catalog.py      # rescan extracted packs      (fast)
python bin/build_search_index.py       # rebuild asset-index.db      (fast)
python bin/build_unity_cache_index.py  # only for new Store downloads (~5 min,
                                       # streams all ~22GB of .unitypackage)
python bin/push_catalog_to_knowledge.py  # refresh knowledge-base entries
```

Adding a new pack means adding its licence row to `PACKS` in
`build_asset_catalog.py` first — a pack with no row is reported as UNKNOWN
licence rather than silently assumed free. That is deliberate: guessing a
licence is the one failure mode with real consequences.
