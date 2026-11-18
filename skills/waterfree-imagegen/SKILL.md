---
name: waterfree-imagegen
description: Use the `waterfree imagegen` CLI to generate images on the local GPU with Stable Diffusion via diffusers. Presets are tuned for a 12 GB card and every setting is configurable per workspace or per invocation.
requiresOllama: false
---

# WaterFree — Image Generation

Generates through local Diffusers or a configured ComfyUI HTTP endpoint.
Inputs are sent to that endpoint when `--backend comfy` is selected.

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
LoRA loading also requires `peft` in that same Python environment.

If `status` reports `shadowing`, or an import fails with an error from some
unrelated script, a loose `.py` file is standing in for a real package (a
scratch `h2.py` once broke `transformers`). **Never write one-off scripts into
the shared venv root `C:\Projects\.local`**: on Windows that folder is on
`sys.path` for every process using the venv. Use a temp dir or
`C:\Projects\.local\scratch\` instead, and move any reported file away.

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

## Editing, LoRAs and ComfyUI

```bash
waterfree imagegen make "a red sailboat" --image boat.png --strength 0.65
waterfree imagegen make "a lake background" --image boat.png --mask background.png --strength 0.8
waterfree imagegen make "misty forest" --preset sdxl --lora C:/models/style.safetensors=0.7
waterfree imagegen status --backend comfy --comfy-url http://127.0.0.1:8188
waterfree imagegen make "a toy boat" --backend comfy --preset sdxl-turbo --checkpoint sd_xl_turbo_1.0_fp16.safetensors
```

Input paths are relative to the current directory. Images resize to the requested
dimensions; masks resize with nearest-neighbor sampling. White edits, black
preserves. Strength must be greater than zero and at most one. Repeat
`--lora PATH=WEIGHT` to combine adapters. Diffusers also accepts Hub repo IDs.
Unsupported pipeline conversions report an error. Sidecars retain mode, input
paths and SHA-256 hashes, adapter weights and resolved settings.

ComfyUI must be running and the checkpoint/LoRAs registered in its configured
model folders. Choose the matching preset for steps and guidance. Select
`--checkpoint` when multiple checkpoints exist. The same editing flags work
with this backend. `--timeout` bounds job polling and reports the job ID on
timeout; it does not interrupt other users' jobs. The client needs no local
CUDA or Diffusers for ComfyUI. ComfyUI CFG=1 corresponds to Diffusers guidance=0;
sidecars record the effective CFG.

Diffusers emits progress JSON on stderr with observed cache byte growth and
incomplete-file sizes; unknown totals remain null. Stdout remains one result
JSON. Set `WATERFREE_IMAGEGEN_PROGRESS=0` to disable progress and
`HF_HUB_OFFLINE=1` for cached-only operation.

### Exit codes

| Code | Meaning |
|------|---------|
| 0    | Images written |
| 2    | Usage error (empty prompt, batch over 8, unknown preset) |
| 4    | Backend unavailable, download failed, gated repo, or out of VRAM |
