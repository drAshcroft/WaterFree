---
name: waterfree-meshgen
description: Use the `waterfree meshgen` CLI to turn an image of a single object into a UV-textured 3D model (GLB with PBR textures) on the local GPU with trellis.cpp (TRELLIS.2). Use when a task needs a 3D prop, item, building or creature model, or when asked to make an image into 3D. Pair with waterfree-imagegen to go from a text idea to a mesh.
requiresOllama: false
---

# WaterFree — Image to 3D (meshgen)

One picture of one object in; a UV-unwrapped GLB with PBR textures out, plus a
`.json` sidecar (input hash, seed, settings, engine and model revision, licence,
triangle count). Runs locally on the GPU with
[trellis.cpp](https://github.com/pwilkin/trellis.cpp) (Microsoft TRELLIS.2-4B,
Q8_0 weights).

Check the asset library first (`waterfree-assets` skill) — an owned model beats
a generated one.

## First run

```bash
waterfree meshgen status      # exit 4 + "fix" if the engine or weights are missing
waterfree meshgen install     # ~0.7 GB engine + ~10 GB weights, once; never re-downloads
```

`make` never downloads anything.

## Text idea → mesh

trellis takes an **image**, not a prompt. Make the image first, pick the best
one, then mesh it:

```bash
waterfree imagegen make "a weathered wooden treasure chest with iron bands, single object, centered, three-quarter view, plain light grey background, soft studio lighting, game asset" -n 4
waterfree vision look .waterfree/generated/*.png --purpose compare     # pick one
waterfree meshgen make .waterfree/generated/<chosen>.png
```

What makes a good input image:

- **One object**, whole and centred, nothing cropped at the edges.
- **Plain, contrasting background** (light grey works well). Background removal
  (BiRefNet) runs automatically; an image that already has alpha keeps it.
- **Three-quarter view** from slightly above shows three sides, so less of the
  back has to be invented.
- Soft, even light. Hard shadows and strong highlights get baked into the
  texture.

## Generate

```bash
waterfree meshgen make chest.png                       # res 1024
waterfree meshgen make a.png b.png c.png --res 512     # quick drafts, one batch
waterfree meshgen make hero.png --atlas 4096 --seed 42
```

| Flag | Default | Notes |
|------|---------|-------|
| `--res 512\|1024\|1536` | 1024 | 512 is ~4× faster and a little softer; 1536 is untested on 12 GB |
| `--atlas PX` | 2048 @1024 | texture size: 512, 1024, 2048, 4096 |
| `--no-texture` | | geometry only |
| `--bg-removal` | auto | `birefnet` or `threshold` to force one |
| `--uv xatlas\|box` | xatlas | box is faster with looser packing |
| `--seed N` | random | recorded in the sidecar either way; re-roll a bad mesh with a new seed |

**Triangle counts are high and fixed.** trellis.cpp v0.8.1 always decimates to
~150K triangles at res 512 and ~300K at res 1024; it has no way to ask for
fewer, so `--faces` is refused. That is fine for a hero prop or a render, but
heavy for a game asset — tell the user, and decimate in Blender (Decimate
modifier, Collapse; the baked texture survives) until WaterFree's own
decimation pass lands. The result JSON reports each mesh's actual `triangles`.

**It is slow — run it in the background.** On the RTX 3060 / i7-4790 this was
built on: res 512 ≈ 2.7 min, res 1024 ≈ 10.7 min per image (most of it CPU-side
remeshing). That is past most agents' foreground command timeout, so use your
harness's background mode and wait for completion. Progress lines (stages, the
sampler bar, a heartbeat every 30 s) go to stderr; the result JSON goes to
stdout. A batch loads the model once.

Output: `<workspace>/.waterfree/generated/meshes/<stamp>_<name>_<id>.glb` + `.json`.
The result lists each mesh's triangle count and time; `failed` lists any image
that did not make it (the rest of the batch still completes).

## Look at the mesh before handing it over

You cannot see a GLB, so render it:

```bash
waterfree meshgen preview .waterfree/generated/meshes/<mesh>.glb      # ~7 s a mesh
waterfree meshgen make chest.png --preview                            # or right after making it
waterfree vision look <mesh>.preview.png --purpose describe           # then check the grid
```

`preview` writes front, three-quarter, side and back PNGs (`<mesh>.preview-<view>.png`)
and a labelled 2x2 grid (`<mesh>.preview.png`), rendered with model-viewer in
headless Chromium (needs Playwright's Chromium; works offline). Look for holes,
a flat or smeared back (trellis invents the unseen side), floating bits, and
whether it is the object that was asked for.

## The GPU is shared

`make` takes the machine-wide **exclusive GPU lease**. If another job (imagegen,
audiogen, another agent's meshgen) holds it, `make` waits in a FIFO queue,
printing who holds it and your position. It then unloads resident Ollama models
and checks free VRAM (needs ~5 GB at res 1024, ~3 GB at 512).

| Flag | Use |
|------|-----|
| `--no-wait` | fail at once (exit 4, holder in the JSON) instead of queueing |
| `--wait-timeout S` | queue for at most S seconds (default 1800) |
| `--skip-vram-check` | start anyway; on Windows an over-budget job crawls instead of failing |

`waterfree gpu status` shows the holder, the queue and free VRAM.

## Licence

TRELLIS.2 and BiRefNet are MIT, but the image encoder (DINOv3) is under
**Meta's DINOv3 License**. Every sidecar records this; check it before shipping
generated models commercially.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | At least one mesh written (see `failed` for the rest) |
| 1 | Every image failed, or the engine crashed (stderr names the engine log) |
| 2 | Bad input: unreadable image, invalid flag value |
| 4 | Not installed, GPU busy past the wait, or too little free VRAM |
