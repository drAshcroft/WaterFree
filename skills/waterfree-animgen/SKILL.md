---
name: waterfree-animgen
description: Use the `waterfree animgen` CLI to generate humanoid character animation clips from text on the local machine with NVIDIA Kimodo — walks, runs, idles, attacks, jumps, deaths, gestures — as BVH and FBX (for Unity/Blender) plus NPZ. Use when a task needs character animation or mocap-style motion for a humanoid, or asks to animate a person. Needs ~17 GB of free RAM the first time each prompt is used, so warn the human first.
requiresOllama: false
---

# WaterFree — Text to motion (animgen)

A text prompt in; a humanoid motion clip out: **BVH** (77-joint SOMA skeleton,
centimetres, Y up, 30 fps, T-pose rest pose), **FBX** made from it with
Blender, Kimodo's **NPZ** (joint positions and rotations), and a `.json`
sidecar (prompt, seed, settings, stats, engine and weight revisions, licences).
Runs [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo) locally.

Check the asset library first (`waterfree-assets` skill): an owned animation
pack (walk, run, melee sets) beats a generated clip.

## ⚠ Warn the human about memory before you run it

Kimodo's text encoder is an 8-billion-parameter language model (Llama 3) that
runs **on the CPU and takes ~15.5 GB of RAM** while it loads and reads the
prompts. Before running `make` with prompts that have not been used before,
**tell the human to close other memory-heavy programs**: a browser with many
tabs, Unity or another game-engine editor, extra editor windows, WSL. Ask them
to say when they have done it.

- The command checks this itself. With less than ~17 GB of free RAM it stops
  (exit 4) before loading anything, and its JSON lists `largestPrograms` with
  how much each holds. Read that list to the human. Do not suggest closing the
  program you, the agent, are running in.
- `--skip-ram-check` runs anyway. Use it only if the human explicitly says to:
  Windows will push their open programs out to disk and the machine will
  stutter for minutes.
- **Each prompt's encoding is cached.** Re-running a prompt that was used
  before (a new seed, a new length, `--in-place`) never loads the encoder and
  needs no extra RAM. `waterfree animgen status` shows free RAM and how many
  prompts are cached.

## First run

```bash
waterfree animgen status      # exit 4 + "fix" if anything is missing
waterfree animgen install     # once: ~5 GB venv + ~16.5 GB weights; never re-downloads
```

`install` needs `uv`, a C++ compiler (Visual Studio Build Tools, "Desktop
development with C++"), and a Hugging Face token (`HF_TOKEN` or `hf auth login`)
whose account has accepted Meta's Llama 3 licence. `make` never downloads and
needs no token.

## Generate

```bash
waterfree animgen make "a person jogs forward"
waterfree animgen make "a person swings a sword overhead with both hands" -n 3   # 3 variants, pick one
waterfree animgen make "a person walks forward" --loop --in-place --seconds 1.2  # a walk cycle
waterfree animgen make "a person walks" --path 0,4 3,6 --seconds 5               # walk a route
waterfree animgen make --prompts clips.json
```

| Flag | Default | Notes |
|------|---------|-------|
| `-n N` | 1 | variants per prompt (1-8), consecutive seeds |
| `--seconds S` | 4 | 0.5-10 s per clip (Kimodo is trained on clips up to 10 s); with `--loop`, the cycle length (max 6) |
| `--loop` | off | a seamless cycle: see Loops below |
| `--in-place` | off | removes the travel across the floor; keeps hip sway and turning |
| `--path X,Z ...` | | waypoints to walk through: see Paths below |
| `--constraints FILE` | | a Kimodo constraints JSON (poses, hand/foot targets) |
| `--seed N` | random | recorded in the sidecar either way |
| `--steps N` | 100 | diffusion steps |
| `--tpose seed` | standard | BVH rest pose: T-pose (what retargeting expects) or the dataset's own |
| `--fbx never\|always` | auto | FBX through Blender when it is installed |
| `--no-preview` | | skip the stick-figure contact sheet PNG |
| `--no-postprocess` | | skip Kimodo's foot-skate clean-up |

A batch file (`.json`) names clips and chains segments into one motion:

```json
[
  {"name": "hero_idle", "text": "a person stands relaxed, breathing, shifting weight", "seconds": 4, "loop": true},
  {"name": "hero_walk", "text": "a person walks forward", "seconds": 1.2, "loop": true, "inPlace": true},
  {"name": "hero_patrol", "text": "a person walks calmly", "seconds": 6, "path": [[0, 4], [4, 4]]},
  {"name": "hero_attack", "segments": [
      {"text": "a person raises a sword", "seconds": 1},
      {"text": "a person swings the sword down hard", "seconds": 1.5}]}
]
```

`name` becomes the file name (`hero_idle.bvh`, `hero_idle.fbx`, ...); with
`-n 3` it is `hero_idle_01` .. `_03`. A `.txt` file is one prompt per line.
A `constraints` file named in a batch file is relative to the batch file.

## Loops

`--loop` makes a seamless cycle of about `--seconds`: Kimodo generates a longer
clip, WaterFree finds the two frames inside it whose poses and velocities match
best, cuts the cycle there, crossfades the seam, and ends the clip on a copy of
its first pose (game engines expect the last key to equal the first). Use it
for idles and walk/run cycles; add `--in-place` for locomotion the game moves.
A loop is one prompt (no segments, path or constraints). The result reports
`loop.cycleSeconds` and `seamStepM` against `typicalStepM`: when they are close,
the wrap is as smooth as any other frame. In Unity, tick **Loop Time** on the clip.

## Paths and constraints

- `--path X,Z ...` (or `"path"` in a batch file): waypoints in metres, reached at
  a steady pace over the clip. The clip starts at 0,0 facing +z; **+x is the
  character's left**. `--path 0,4 3,6` walks forward 4 m, then veers left. Kimodo
  follows the path even when the prompt says little; the prompt sets the style.
- `--constraints FILE`: Kimodo's own constraints JSON (the format its web demo
  saves: full-body keyframes, hand/foot targets, root paths). Use it when text
  alone misses, e.g. pinning both hands on a sword at the top of a swing.

## Previews: look before you hand a clip over

Each clip gets `<name>.png`: front and side stick figures at eight moments plus
a top-down root path (left limbs red, right blue). Check it with
`waterfree vision look <name>.png --purpose describe`, or read it yourself:
does the person do what was asked, with the right hands and feet?

## Writing prompts

- Describe a **person** and one action, plainly: "a person crouches and sneaks
  forward", "a person gets hit, staggers back and falls". Kimodo is trained on
  human motion capture; it does not animate animals, creatures or props.
- It follows the gist better than specifics: "both hands", exact counts and
  styles are hit or miss. Generate `-n 3` and pick, or split the action into
  segments.
- For longer motion, chain segments (each up to 10 s) rather than one long prompt.

## Reading the result

Each clip in the JSON result has `frames`, `seconds`, `rootTravelM` (how far it
moved; with `--in-place` this is the distance the game must move the character),
`preview`, and `loopGapM`: the average distance between the first and last pose
(for a clip made without `--loop`; 0.2 m or more means it does not loop as is).
`vramSpill` is non-zero only if the job overflowed the GPU into system RAM.

**Timing** on the machine this was built on (RTX 3060, i7-4790): a batch with
new prompts spends ~2 min loading the text encoder, then ~10-40 s per clip; a
batch of cached prompts starts in ~25 s and takes ~10 s per clip. A big batch
can exceed a foreground command timeout: use your harness's background mode.
Progress goes to stderr, the result JSON to stdout. FBX conversion runs after
the GPU is released (~15 s for the whole batch).

## Using the clips

- **Unity**: import the `.fbx` and set its Rig to **Humanoid**. Verified on
  Unity 6 (2026-11-18): the avatar is valid with all required bones and 55 in
  total mapped (fingers, eyes and jaw included), at scale 1, as a 30 fps human
  clip named after the file; it then retargets onto any Humanoid character.
  Tick **Loop Time** for `--loop` clips.
- **Blender**: import the `.bvh` (scale 0.01, and tick "Update Scene FPS" or the
  clip plays 25% slow) or the `.fbx`.
- The motion is on Kimodo's own skeleton. Putting it on a character means
  retargeting (Unity Humanoid, Blender, or a retargeting tool); a mesh from
  `waterfree meshgen` is not rigged, so it cannot use these clips directly.

## The GPU is shared

`make` takes the machine-wide exclusive GPU lease (it needs only ~2.5 GB of
VRAM), queues behind other GPU jobs, and unloads Ollama models first. `--no-wait`,
`--wait-timeout S`, `--skip-vram-check` and `--quiet` work as in the other
generators. `waterfree gpu status` shows who holds it.

## Licence

Kimodo code Apache-2.0; Kimodo-SOMA-RP weights under the NVIDIA Open Model
License (commercial use allowed); the text encoder is Meta Llama 3 (Llama 3
Community License) with MIT LLM2Vec adapters. Every sidecar records this.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | At least one clip written (see `failed` for the rest) |
| 1 | Every clip failed, or the worker crashed (stderr names its log) |
| 2 | Bad input: empty prompt, length out of range, malformed batch file |
| 4 | Not installed, too little free RAM (`insufficientRam`, with `largestPrograms`), GPU busy past the wait, or too little VRAM |
