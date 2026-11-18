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
waterfree imagegen status     # is the backend usable, and is the preset installed?
waterfree imagegen models     # presets, disk cost, and what fits this GPU
waterfree imagegen install    # the workspace's preset; or: install sdxl-turbo, install all
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

**`make` never downloads.** Weights (7–24 GB depending on preset) arrive
through `imagegen install`, into the Hugging Face cache, not the workspace;
`install` fetches only what the pipeline loads and skips anything already
cached. An uninstalled preset makes `make` exit 4 naming the install command.
SD3.5 and FLUX are *gated*: accept the licence on the model's HF page and set
`$HF_TOKEN` before installing, or the download 401s.

## Presets

A preset is a known-good set of six coupled settings — weights, steps, guidance,
resolution, dtype, offload. Getting one wrong on a 12 GB card means an OOM or a
washed-out image.

| Preset         | Disk   | Defaults          | Use it for |
|----------------|--------|-------------------|------------|
| `sd35-medium`  | ~11 GB | 28 steps, cfg 4.5, 1024² | **Default.** Best prompt adherence and legible in-image text that fits 12 GB |
| `sdxl`         | ~7 GB  | 30 steps, cfg 7.0, 1024² | Lighter and faster; weaker at text |
| `sdxl-turbo`   | ~7 GB  | 2 steps, cfg 0, 512²     | Sub-second drafts while iterating on a prompt |
| `flux-schnell` | ~24 GB | 4 steps, cfg 0, 1024²    | Highest quality — needs `--offload sequential` on a 12 GB card and is slow |

Free VRAM each needs, **measured** on an RTX 3060 12 GB (one image at the
preset's size; the preflight uses these numbers):

| Preset | `--offload none` | `model` (default) | `sequential` |
|--------|------------------|-------------------|--------------|
| `sd35-medium` | does not fit (crashed) | 10.3 GB — the whole card | 3.3 GB, ~1.7× slower |
| `sdxl` | does not fit (spilled 3 GB into RAM) | 7.9 GB | 3.4 GB, ~3.4× slower |
| `sdxl-turbo` | 9.2 GB | 5.6 GB | 1.0 GB |
| `flux-schnell` | ~22 GB (not measured) | ~22 GB | ~3 GB |

`sd35-medium` with the default model offload uses everything a 12 GB card has
free next to the desktop: close GPU-heavy programs first, or use `sdxl`.
`fitsGpu` in `imagegen models` is advisory, not a gate. If a run outgrows the
card anyway, the result's `vramSpill` says so (the job ran slowly in system RAM).

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

Each `make` is its own process: it loads the pipeline (~30-70 s), generates,
and frees the GPU when it exits. Ask for several images in one call (`-n 4`)
rather than four calls.

### Configure a workspace
```bash
waterfree imagegen init      # writes .waterfree/imagegen.json with every setting
```
Settings resolve in three layers, later winning: **preset defaults → 
`.waterfree/imagegen.json` → CLI flags**. A malformed config falls back to
defaults rather than erroring.

## The GPU is shared

`make` takes the machine-wide **exclusive GPU lease**. If another job
(meshgen, audiogen, another agent's imagegen) holds it, `make` waits in a FIFO
queue, printing the holder and your position on stderr. Once it has the GPU it
unloads resident Ollama models (they otherwise hold 6-9 GB for up to 30 min
after a vision/qa call) and checks free VRAM against the preset's need.

| Flag | Use |
|------|-----|
| `--no-wait` | fail at once (exit 4, holder in the JSON) instead of queueing |
| `--wait-timeout S` | queue for at most S seconds (default 1800) |
| `--skip-vram-check` | start anyway; on Windows an over-budget run spills into system RAM and crawls |

`waterfree gpu status` shows who holds the GPU and who is waiting.
`waterfree imagegen unload` (= `waterfree gpu evict`) makes room by unloading
Ollama models. With the ComfyUI backend, `--comfy-free` (or `comfyFreeAfter`
in imagegen.json) asks a ComfyUI on this machine to unload its models after the
job; ComfyUI keeps them loaded otherwise, starving the next GPU job.

## When you run out of VRAM

The error names the ladder; work down it:

1. `--offload sequential` — largest saving, slowest
2. `--width 768 --height 768` — VRAM scales with area
3. `--preset sdxl-turbo` — lightest preset

If the preflight refuses with "too little free VRAM", the error lists the other
programs using the GPU (browsers, games, other AI tools); close the heavy ones.

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
| 4    | Backend unavailable, preset not installed, gated repo, GPU busy past the wait, or too little VRAM |

## Next step: 3D

An image of one object on a plain background can become a textured 3D model:
see the `waterfree-meshgen` skill.
