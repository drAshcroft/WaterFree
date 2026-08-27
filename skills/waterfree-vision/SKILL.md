---
name: waterfree-vision
description: Use the `waterfree vision` CLI to look at screenshots and images with a local vision model — describe a rendered page, critique its UI, read its text, infer what it does, or compare two versions. Runs on the local GPU via Ollama; no image ever leaves the machine.
requiresOllama: true
---

# WaterFree — Vision

Analyzes images with a local vision model through the Ollama daemon WaterFree
already uses. Nothing is uploaded anywhere.

Each invocation is a short shell command — run it in whatever shell you have
(Bash or PowerShell). `waterfree` is on PATH. All commands emit JSON to stdout.

## When to Use

- Check that a page you just generated renders correctly — `--purpose triage`
- Critique the layout of a screen — `--purpose ui`
- Work out what an unfamiliar page is for — `--purpose function`
- Read text out of a screenshot — `--purpose text`
- Compare a before/after — `--purpose compare`

**Give it a file path.** This skill does not take screenshots for you: capture
the image first (any screenshot tool, or Playwright, which is available at
`C:\Projects\.local`), then point `vision look` at the file.

## Purposes

The purpose picks both the model tier and the framing of the question. Run
`waterfree vision purposes` for the live table.

| Purpose    | Tier  | What it gives you |
|------------|-------|-------------------|
| `describe` | small | Short factual description of the image |
| `triage`   | large | Verdict on whether a rendered page looks intact |
| `ui`       | large | Layout, spacing, contrast, accessibility notes |
| `function` | large | What the page is for and what a user can do on it |
| `text`     | large | Verbatim transcription of on-screen text |
| `compare`  | large | Concrete differences between two or more images |

## Models

Two tiers, and **nothing is ever downloaded implicitly** — a missing model is an
error that names the exact pull command, because these are gigabytes.

| Tier    | Model          | Disk    | Good for |
|---------|----------------|---------|----------|
| `small` | `moondream`    | ~1.7 GB | Open-ended description, bulk passes |
| `large` | `qwen2.5vl:7b` | ~6.0 GB | Everything analytical |

```bash
waterfree vision models              # what exists, what is downloaded
waterfree vision pull --tier small   # ~1.7 GB
waterfree vision pull --tier large   # ~6.0 GB
```

Swap in any other Ollama vision model per-invocation with `--model llava:13b`,
or permanently via `$WATERFREE_VISION_MODEL_SMALL` / `$WATERFREE_VISION_MODEL_LARGE`.
The tier is the contract; the model id behind it is yours to choose.

### The small tier answers questions, it does not classify

`moondream` returns an **empty string** for yes/no questions and for prompts that
demand a verdict token ("Is this correct?", "answer OK or BROKEN"). It answers
open "describe / what is / what problems" questions reliably. That is why
`triage` sits on the large tier despite being a cheap-sounding job. If you point
`--model moondream` at an analytical purpose, expect blanks — that is the model,
not a bug.

## CLI

### Look at an image
```bash
waterfree vision look screenshot.png --purpose ui
```
```json
{
  "purpose": "ui",
  "model": "qwen2.5vl:7b",
  "question": "Review this interface. Cover: visual hierarchy, ...",
  "answer": "...",
  "images": [
    { "path": "...", "sent": "1280x720", "original": "2560x1440", "downscaled": true }
  ]
}
```

### Ask something specific
```bash
waterfree vision look shot.png --purpose ui -q "Is the primary action obvious?"
```
`-q` replaces the purpose's default question but keeps its system framing and
model tier.

### Compare versions
```bash
waterfree vision look before.png after.png --purpose compare
```
Up to 4 images go into a single turn, so the model sees them together. They are
numbered in the prompt (`[image 1] before.png`), so the answer can refer to them
unambiguously.

## Image handling

Images are downscaled to 1280px on the long edge before being sent. This is not
cosmetic: a 4K screenshot costs thousands of image tokens and most of the card's
headroom for detail the model does not use. Re-encoded as PNG, never JPEG —
JPEG artefacts around small UI text are exactly what breaks transcription.

Accepted: `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`, `.bmp`.

Tunable via environment: `WATERFREE_VISION_MAX_EDGE` (default 1280),
`WATERFREE_VISION_MAX_IMAGES` (4), `WATERFREE_VISION_TIMEOUT` (300s),
`WATERFREE_VISION_KEEP_ALIVE` (15m — keeps the model resident so the second
image is fast).

## VRAM

The large tier and a 14B text model do not both fit comfortably on a 12 GB card,
and neither does image generation. If a call is slow or fails, free the GPU:

```bash
ollama stop qwen2.5:14b
waterfree imagegen unload
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Analysis returned |
| 2    | Usage error (unknown purpose, too many images) |
| 4    | Model not downloaded, image unreadable, or Ollama unreachable |
