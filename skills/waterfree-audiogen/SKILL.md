---
name: waterfree-audiogen
description: Use the `waterfree audiogen` CLI to make game audio on the local GPU with openmoss — design NPC voices from a text description, speak dialogue lines in a saved character voice (consistent across hundreds of lines), and generate sound effects from text. Use when a task needs voice acting, NPC barks, dialogue audio, a character voice, or SFX.
requiresOllama: false
---

# WaterFree — Voices, dialogue and sound effects (audiogen)

Runs [openmoss](https://github.com/pwilkin/openmoss) locally on the GPU. Three
models, one per job:

| Job | Model | Output |
|-----|-------|--------|
| `design` a voice from a description | MOSS-VoiceGenerator 1.7B | 24 kHz mono |
| `speak` lines in a saved voice | MOSS-TTS-Local v1.5 (Q8_0) | 48 kHz stereo |
| `sfx` from a description | MOSS-SoundEffect v2.0 | 48 kHz mono |

Check the asset library first (`waterfree-assets` skill) for owned audio.

## First run

```bash
waterfree audiogen status                 # which roles are installed; exit 4 + "fix" if none
waterfree audiogen install                # all three (~24 GB), once; never re-downloads
waterfree audiogen install voice speech   # or just the roles you need
```

## NPC voices: design → save → speak

A character must sound like the same person in every line. Designing from a
description gives a *different* person each time, so design once, keep the
take you like as a named voice, and clone that voice for every line.

```bash
# 1. Three candidate voices speaking a sample line (6-15 s of speech makes a good reference)
waterfree audiogen design "Hearty, jovial tavern owner, loud and welcoming, slightly gruff, American English" \
    --text "Welcome to the Rusty Anchor, traveller! Pull up a stool, the stew is hot and the ale is cold."

# 2. Listen, then keep one (description and transcript are taken from its sidecar)
waterfree audiogen save-voice innkeeper --from .waterfree/generated/audio/voice-design/<chosen>.wav

# 3. Speak lines in that voice
waterfree audiogen speak --voice innkeeper "Rooms are upstairs. Mind the third step, it bites."
waterfree audiogen speak --lines dialogue.json
```

Describe voices the way the model was trained: who they are, age, timbre,
energy, accent. E.g. "Tired, raspy old woman speaking slowly, faint wheeze" or
"Young, eager squire, quick and breathless, British English". Generate 3-4
candidates; they differ a lot.

You can also save a voice from any clean recording you have the rights to:
`save-voice name --from recording.wav --transcript "words spoken"` (2-60 s).

### Batch dialogue

`--lines` takes a `.txt` (one line each) or `.json`. JSON entries can switch
voices and name their output files, so a whole scene is one command (and one
model load):

```json
[
  {"voice": "innkeeper", "name": "inn_greet",  "text": "Another round? Coin first, friend."},
  {"voice": "guard",     "name": "guard_warn", "text": "Keep your blade sheathed in here."},
  {"voice": "innkeeper", "name": "inn_rumour", "text": "Wolves on the north road again, they say."}
]
```

Every voice is checked before the GPU is touched, so a typo fails at once.
Saved voices live in the project at `.waterfree/voices/<name>.wav + .json`;
commit them with the game if they are part of it.

`--channels mono` downmixes for positional (3D) audio in a game engine;
`native` (default) keeps the model's own layout. `--instruction "whispering"`
passes a style hint. `--seed N` makes takes reproducible (line k uses N+k).

## Sound effects

```bash
waterfree audiogen sfx "heavy wooden door creaks open slowly, stone hall reverb" --seconds 3 -n 3
waterfree audiogen sfx --prompts sfx.txt --seconds 2
```

Describe the source, the action and the space: "sword drawn from a leather
scabbard, bright metallic ring", "distant thunder rolling over open fields".
`--seconds` is at most 30. `-n` gives variants (consecutive seeds) to choose
from or to rotate in-game.

**SFX is slow**: the model always denoises a full 30 s of audio, whatever
`--seconds` says, so expect ~3 minutes per clip on a 12 GB card. Run batches in
the background. `--cfg 1` halves the time at some cost in prompt adherence;
`--steps 50` is faster and rougher.

## Speed (RTX 3060 12 GB)

| Job | Model load | Then |
|-----|-----------|------|
| design | ~20-35 s | ~2 s per candidate |
| speak | ~20 s | ~1.6× faster than real time |
| sfx | ~18 s | ~2.7 min per clip |

Each command loads its model once, so batch your lines and prompts.

## Checking takes automatically (`--qa`, opt-in)

Add `--qa` to `design`, `speak` or `sfx` to have every take checked after
generation, in the same GPU slot (one-time setup: `waterfree audiogen install qa`,
~4.5 GB venv; the Whisper and CLAP models are ~3.5 GB):

- **Speech and voice design**: Whisper large-v3 transcribes each take and
  compares it with the requested words. Each file gets `qa.wer`, `misread`
  (wrong word, e.g. `["stew", "stool"]`), `dropped`, `added`, `repeated`
  (stutters) and `spellingVariants` (traveller/traveler, not an error).
  `qa.review: true` means listen to it. Design candidates get a `rank`:
  keep rank 1 as the reference voice.
- **SFX**: CLAP scores how well each variant matches its prompt and ranks the
  `-n` variants of each prompt (`rank` 1 = best match). The raw score is only
  meaningful against other takes of the same prompt.

The result's `qa` lists the takes to `review` and the `best` of each ranked
group. It adds ~25 s of model loading plus ~1 s per take.

## The GPU is shared

Every generating action takes the machine-wide **exclusive GPU lease**: if
another job holds it you wait in a FIFO queue (progress on stderr names the
holder). The job then unloads resident Ollama models and checks free VRAM.
Free VRAM needed: design 8.9 GB, sfx 9.3 GB, speak 10.3 GB — `speak` needs
nearly the whole card, so close GPU-heavy programs (browsers with video, games)
if it refuses. Do not reach for `--skip-vram-check` to force it: a speech model
that does not fit spills into system RAM and runs ~13× slower.

`--no-wait`, `--wait-timeout S`, `--skip-vram-check` and `--quiet` work as in
the other GPU areas; `waterfree gpu status` shows the holder and the queue.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | At least one file written (see `failed` for the rest) |
| 1 | Everything failed, or the engine crashed (stderr names the engine log) |
| 2 | Usage: unknown voice, bad lines file, invalid value |
| 4 | Not installed, GPU busy past the wait, or too little free VRAM |

All models are Apache-2.0.
