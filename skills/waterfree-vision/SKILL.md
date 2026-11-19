---
name: waterfree-vision
description: Use the `waterfree vision` CLI to look at screenshots and images with a local vision model — describe a rendered page, critique its UI, read its text, infer what it does, compare two versions, or classify a render/sprite/photo against a fixed label list as checkable JSON. Runs on the local GPU via Ollama; no image ever leaves the machine.
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
- Blind identity check of a render, sprite or photo ("does this read as a Greek
  island or as Japan?") — `--purpose classify --labels a,b,c`

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
| `classify` | large | One label from `--labels`, as JSON `{label, evidence, confidence}` — any image, not just web pages |

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
  "tier": "large",
  "tierReason": "default for purpose 'ui'",
  "question": "Review this interface. Cover: visual hierarchy, ...",
  "answer": "...",
  "advisory": true,
  "warnings": [],
  "images": [
    { "path": "...", "sent": "1280x720", "original": "2560x1440", "downscaled": true }
  ]
}
```
`tierReason` says why that model answered. `warnings` is always present; each
entry has a `code` and a `message` — read them before trusting the answer.
`advisory: true` (compare, triage, ui, classify) means the answer is one 7B
model's judgement: inspect the image yourself before accepting it.

### Ask something specific
```bash
waterfree vision look shot.png --purpose ui -q "Is the primary action obvious?"
```
`-q` replaces the purpose's default question but keeps its system framing. It
also moves `describe` from the small to the large tier, because the small tier
agrees with whatever a question names. `--tier small` or `--model` override that.

### Ask neutrally — leading questions get a warning
A model asked "is any medieval item visible (trestle table, goblet...)?" tends
to find a trestle table and a goblet: moondream did exactly that for renders
containing neither. When the answer gates a review, that is a false pass. A
question that names candidates — a parenthesised list, "such as", "e.g.",
"X or Y or Z", "A, B or C", "is there a ...", "can you see a ..." — still gets
answered, but carries a `leading-question` warning.

| Leading (warned) | Neutral |
|---|---|
| `Is there a goblet on the table?` | `Describe each object on the table.` |
| `Is it Greek, Japanese or Norse?` | `--purpose classify --labels Greek,Japanese,Norse` |
| `Is any medieval item visible (trestle table, goblet...)?` | `Describe the floor, each piece of furniture and the lamps.` |

Then check the answer for the item yourself. One "or" ("floating or on the
floor?") is not flagged, and neither are lists of aspects to describe
("describe the roof, walls or doors").

### Classify against fixed labels
```bash
waterfree vision look render.png --purpose classify --labels "South of France,Greek island,Scandinavia,Japan,England"
```
```json
{
  "purpose": "classify",
  "labels": ["South of France", "Greek island", "Scandinavia", "Japan", "England", "none", "uncertain"],
  "labelOrder": ["Japan", "England", "Greek island", "South of France", "Scandinavia", "none", "uncertain"],
  "label": "Greek island",
  "evidence": "Whitewashed cubic houses with flat roofs and blue doors on a rocky slope.",
  "confidence": "high",
  "attempts": 1,
  "answer": "{...the model's raw reply...}",
  "advisory": true,
  "warnings": []
}
```
- `--labels` is required: 2–20 comma-separated labels. `none` and `uncertain`
  are always added — a model with no way out picks the nearest label, and a
  gate reads that as a pass. Treat both as "not a match".
- The order is shuffled on every call (recorded in `labelOrder`) so a batch of
  renders is not biased toward whichever label is listed first.
- The reply is constrained to that JSON shape and validated. A label outside
  the list gets one stricter retry; if that also fails, `label` is
  `uncertain`, `answer` holds the raw reply and a `classify-unusable-answer`
  warning says why.
- `-q` sets the question ("Which region's village architecture does this
  resemble?"); keep the candidates in `--labels`, not in the question.
- One image per call. For a robust read, classify several views (overview,
  near, street) and read the `evidence` — it names the cue that misled the
  model, which is usually exactly what to change.

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

## Busy host

When many agents, test suites or game engines share the machine, Ollama can be
slow to answer. Vision degrades rather than failing:

- `--timeout SECONDS` (look and web; default 300, or `$WATERFREE_VISION_TIMEOUT`)
  bounds each attempt at the model. Raise it on a busy host — and give your own
  shell command a matching timeout.
- A timed-out attempt, or Ollama's 503 "server busy", is retried with backoff
  (`$WATERFREE_VISION_RETRIES`, default 1 retry, at most 3). So the worst case
  is about `(retries + 1) × --timeout`.
- The "is the model downloaded?" check waits at most
  `$WATERFREE_VISION_LIST_TIMEOUT` (10 s). If Ollama is too busy to answer it,
  the request goes ahead anyway with a `model-check-skipped` warning; a missing
  model is still reported, by the generation call.
- `--model <id>` skips that check entirely.
- If every attempt times out, the error says so — `Ollama did not answer in N s
  (...); the host may be under load ...` — and names `--timeout`. That is a
  load problem, not a missing model: wait, raise `--timeout`, or free the GPU.
- `waterfree vision models` reports `installed: null` (unknown), not `false`,
  when Ollama cannot be asked — do not pull a model just because of that.

## VRAM

The large tier and a 14B text model do not both fit comfortably on a 12 GB card,
and neither does image generation. If a call is slow or fails, free the GPU:

```bash
ollama stop qwen2.5:14b
waterfree imagegen unload
```

## Exit codes

Multi-image requests budget their image patches against the model context and
downscale further when needed. `WATERFREE_VISION_CONTEXT` controls the requested
context (8192 tokens by default); keep it within your machine's memory budget.
Comparison responses are advisory. Inspect images directly for acceptance and
use focused crops when details are too small to establish overlap confidently.

| Code | Meaning |
|------|---------|
| 0    | Analysis returned |
| 2    | Usage error (unknown purpose, too many images, classify without `--labels`, bad `--timeout`) |
| 4    | Model not downloaded, image unreadable, Ollama unreachable, or Ollama did not answer in time |

## Websites

```bash
waterfree vision web http://localhost:3000 --purpose ui
waterfree vision web http://localhost:3000 --mobile --ready-selector "#app-ready"
waterfree vision web http://localhost:3000 --capture-only
```

Desktop analysis uses a 1280x800 viewport. `--mobile` adds an independent
390x844 capture and analysis. Viewport and full-page PNGs are retained under
`.waterfree/vision-web`; full-page analysis requires `--full-page` so stitched
pages are not mistaken for a single screen. `--output-dir` selects a destination.
Navigation waits for DOM readiness, fonts, and 750ms settling; use
`--ready-selector` for a SPA's meaningful ready state and `--wait-ms` to change
settling time. `--timeout-ms` (page loading) defaults to 30000; the model call
is bounded separately by `--timeout` (seconds). `web` takes the same
`--purpose`, `-q`, `--model`, `--tier` and `--labels` as `look`. Capture
requires Playwright and Chromium; image analysis still runs locally through
Ollama.
