---
name: waterfree-imagegen
description: Use the `waterfree imagegen` CLI to generate images on the local GPU with Stable Diffusion via diffusers. Presets are tuned for a 12 GB card and every setting is configurable per workspace or per invocation.
---

# WaterFree — Image Generation

Generates images locally through `diffusers`. Nothing is sent to a hosted API.

Each invocation is a short shell command — run it in whatever shell you have.
All commands emit JSON to stdout; the images land on disk.

## When to Use

- Generate reference or placeholder imagery for a project
- Iterate on a prompt quickly (`--preset sdxl-turbo`)
- Produce a final image at quality (`--preset sd35-medium`)

## First run

```bash
waterfree imagegen status     # is the backend usable?
waterfree imagegen models     # presets, disk cost, and what fits this GPU
```

`status` exits **4** with the fix if something is missing. Requirements:
`diffusers`, `accelerate`, `sentencepiece`, `protobuf`, plus a CUDA-capable
PyTorch. CPU generation is deliberately not supported — it is minutes per image.

**Weights download on first use** (7–24 GB depending on preset) into the
Hugging Face cache, not the workspace. SD3.5 and FLUX are *gated*: accept the
licence on the model's HF page and set `$HF_TOKEN`, or the download 401s.

## Presets

A preset is a known-good set of six coupled settings — weights, steps, guidance,
resolution, dtype, offload. Getting one wrong on a 12 GB card means an OOM or a
washed-out image.

| Preset         | Disk   | VRAM   | Defaults          | Use it for |
|----------------|--------|--------|-------------------|------------|
| `sd35-medium`  | ~11 GB | ~9.5GB | 28 steps, cfg 4.5, 1024² | **Default.** Best prompt adherence and legible in-image text that fits 12 GB |
| `sdxl`         | ~7 GB  | ~8 GB  | 30 steps, cfg 7.0, 1024² | Lighter and faster; weaker at text |
| `sdxl-turbo`   | ~7 GB  | ~6.5GB | 2 steps, cfg 0, 512²     | Sub-second drafts while iterating on a prompt |
| `flux-schnell` | ~24 GB | ~22 GB | 4 steps, cfg 0, 1024²    | Highest quality — needs `--offload sequential` on a 12 GB card and is slow |

`fitsGpu` in `imagegen models` is advisory, not a gate: offloading trades speed
for headroom and can rescue a preset that "doesn't fit".

**Distilled presets (`sdxl-turbo`, `flux-schnell`) run at guidance 0** and have
no unconditional branch, so `--negative-prompt` is ignored for them. Passing a
normal guidance scale to a distilled model produces burnt, oversaturated output.

## CLI

### Generate
```bash
waterfree imagegen make "a cutaway diagram of a water filtration tank, technical illustration"
waterfree imagegen make "logo concept, minimal, flat" -n 4 --preset sdxl-turbo
waterfree imagegen make "misty forest at dawn" --seed 12345 --steps 40
```
Output:
```json
{
  "prompt": "...",
  "count": 1,
  "outputDir": "C:\\...\\.waterfree\\generated",
  "files": [ { "image": "...png", "metadata": "...json" } ],
  "settings": { "preset": "sd35-medium", "steps": 28, "size": "1024x1024", ... },
  "timing": { "loadSeconds": 31.4, "generateSeconds": 18.2 }
}
```

Every image gets a sidecar `.json` with the prompt, seed, and full settings —
without it a promising image is unreproducible five minutes later. Note that a
batch shares one generator, so **only image 1 is reproducible from the recorded
seed**; the rest record `null` rather than a seed that would not reproduce them.

Per-invocation overrides: `--preset --steps --guidance --width --height
--negative-prompt --seed --offload --count --output-dir --hf-token`.

### Configure a workspace
```bash
waterfree imagegen init      # writes .waterfree/imagegen.json with every setting
```
Settings resolve in three layers, later winning: **preset defaults → 
`.waterfree/imagegen.json` → CLI flags**. A malformed config falls back to
defaults rather than erroring.

### Free the GPU
```bash
waterfree imagegen unload
```
The pipeline is cached in-process, so a second `make` skips the ~30 s load. That
cache also holds ~10 GB of VRAM.

## When you run out of VRAM

The error names the ladder; work down it:

1. `--offload sequential` — largest saving, slowest
2. `--width 768 --height 768` — VRAM scales with area
3. `--preset sdxl-turbo` — lightest preset

A resident Ollama model holds several GB. Free it first: `ollama stop <model>`.

## Reviewing what you generated

Pair with the `waterfree-vision` skill to close the loop:
```bash
waterfree imagegen make "app icon, flat, blue" -n 4
waterfree vision look .waterfree/generated/*.png --purpose compare
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Images written |
| 2    | Usage error (empty prompt, batch over 8, unknown preset) |
| 4    | Backend unavailable, download failed, gated repo, or out of VRAM |
