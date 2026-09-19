---
name: waterfree-qa
description: Use the `waterfree qa` CLI to have a local model actually use a running web app or HTML game the way a careless player would, and report what broke or confused it. Use when asked to QA, playtest, smoke-test or "try" a running app, to find usability problems, or to check a build before release. Needs a URL you have already started; it never starts a server.
---

# WaterFree — QA Agent

A local model drives a real browser through your app one action at a time and
reports what broke and what confused it. The harness owns Playwright; the model
only picks the next action, so a 9B model on your own GPU is enough and a sweep
costs nothing but time.

Two kinds of finding come out, and they come from different places:

- **Technical bugs** are found by the harness, deterministically — console
  errors, uncaught exceptions, failed requests, 4xx/5xx, controls clipped or
  off screen. No model judgement involved.
- **Confusion** is found by the model. A tester who cannot work out what to do
  next has found a real problem, so `STUCK` is a result, not a failure.

## Before you start

**You must start the app yourself.** `qa run` takes a URL and nothing else — it
will not launch a dev server, and it refuses anything that is not an http(s)
URL you passed whole.

```bash
waterfree qa doctor          # Playwright, Chromium, driver + vision models, personas
```
Exit `4` means something is missing; the JSON carries the exact command to fix
each one. Nothing is ever downloaded implicitly.

## Run a sweep

```bash
waterfree qa run http://localhost:5173 \
  --goal "start a new game and finish the first turn" \
  --persona first-timer --persona impatient \
  --steps 40 --minutes 10
```

`--goal` is required: the tester needs something to try. Everything else has a
default.

| Flag | Meaning |
|---|---|
| `--persona NAME` | Repeatable. Default `first-timer`. |
| `--hint "..."` | Repeatable free text. `--hint genre:card-game` pulls a playbook from the knowledge base. |
| `--steps N` / `--minutes N` | Caps per persona. Default 40 / 10. |
| `--viewport WxH` | Override the persona's viewport. |
| `--headed` | Watch it work. |
| `--vision-every N` / `--no-vision` | Screenshot-vs-DOM checks. Default every 5 steps. |
| `--model ID` | Force an Ollama driver model. |
| `--out DIR` | Run root. Default `<workspace>/qa-runs`. |

Output goes to `<out>/<slug>-<date>/<persona>-<n>/` with `run.json`,
`log.jsonl`, `findings.md` and `shots/`. The JSON on stdout summarises every
run and the severity totals.

**Exit code 0 means the run completed, not that the app is fine.** Findings are
data, not failures. Read `findings` in the JSON.

## Verify before you believe

In the baseline this replaces, one in four HIGH findings did not survive
verification. Every finding carries its repro steps, so replay them:

```bash
waterfree qa verify ./qa-runs/myapp-2026-09-18
```

Each finding comes back `confirmed`, `unreproduced`, or `error`, and
`findings.md` is rewritten with the status attached. `unreproduced` means "not
shown again", not "false" — timing-dependent bugs live there.

**Do not report a finding to the user as a bug before verifying it.**

## Consolidate a sweep

```bash
waterfree qa report ./qa-runs/myapp-2026-09-18 --out qa-reports/sweep.md
```

Groups similar findings across personas. A finding two personas tripped over
independently is ranked above one only a single persona saw — independent
rediscovery is the cheapest corroboration you get. Confirmed sorts above
unreproduced.

Accepts a run directory, a sweep directory, or the runs root.

## Personas

```bash
waterfree qa personas
```

| Name | Traits |
|---|---|
| `first-timer` | Reads nothing, clicks the biggest button, expects to be taught |
| `impatient` | Never waits, double-clicks, skips dialogs |
| `careful` | Reads all copy, follows instructions literally |
| `keyboard-only` | Tab, Enter, Space, Escape, arrows. Mouse forbidden |
| `phone` | 390x844, tap targets, clipped strips |
| `rules-lawyer` | Tries to break the rules: illegal moves, undo abuse |

Add your own as `.waterfree/qa/personas/<name>.md` — front matter for
`viewport`, `pacing`, `forbid`, `tags`; the body becomes a prompt fragment. A
workspace file overrides a built-in of the same name.

Stack several in one command; each gets its own browser and run directory.

## Helping the tester

Put a `QA.md` in the repo: entry points, screen names, gotchas, a known-good
opening. It is read verbatim into the prompt, so keep it under ~150 lines.
`--goal` and `--hint` stack on top.

## What it cannot do

- **Canvas-only games.** The snapshot is DOM-based. A Phaser or pure-canvas
  game with an empty DOM gives the model nothing to click. Paradoxia is the
  known example. Vision-driven region clicking is planned, not built.
- **Online multiplayer.** Out of scope.
- **Starting your server.** By design.

## Tuning

`WATERFREE_QA_MODEL`, `WATERFREE_QA_MODEL_FALLBACK`, `WATERFREE_QA_STEP_TIMEOUT`,
`WATERFREE_QA_KEEP_ALIVE`. The fallback tier is used automatically when the
default model stops producing parseable actions.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | The run completed. Findings may still exist — read them. |
| 2 | Usage error (no goal, bad URL, unknown persona) |
| 4 | Setup problem: Playwright, Chromium or a model is missing |
