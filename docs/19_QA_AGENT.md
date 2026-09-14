# Subsystem 19 — QA Agent (`waterfree qa`)
## WaterFree VS Code Extension

---

## Purpose

Exercise a running web app or HTML game the way a real, slightly careless user
would, and hand back a report a developer can act on. Runs are cheap enough to
repeat constantly because the driver is a local Ollama model, and the harness
does the heavy lifting so a small model can succeed.

This replaces the `FlighterPirates-qa-baseline` approach, where Haiku and
Sonnet agents wrote ~6,000 lines of throwaway Playwright scripts per sweep.
That loop found real bugs but burned tokens on script authoring, and one in
four of its HIGH findings did not survive verification.

---

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| Who writes Playwright | Nobody. The harness owns it. | Script authoring was the token sink. The model only chooses actions. |
| Model interface | Numbered element list + short page text in, one constrained-text action out | SWE-agent's lesson: the agent-computer interface matters more than the model. Small models parse and emit this reliably; tool-calling is flakier under Ollama. |
| Driver model | `qwen3.5:9b` default, `qwen2.5:14b` fallback, env-overridable | Both already pulled. Nothing is ever downloaded implicitly (same rule as vision). |
| Vision role | Verification, not navigation | Every N steps and at every finding, `qwen2.5vl:7b` is asked whether the screenshot matches what the DOM claims. Cheap insurance against DOM-says-visible, screen-says-blank. |
| Canvas games | Out of scope for phase 1, explicit fallback in phase 3 | Paradoxia is a Phaser canvas with an empty DOM. DOM-first covers goblinchess (`to_dom`) and the party client today. |
| Bug finding split | Harness finds technical bugs deterministically; model finds confusion | Console errors, failed requests, clipped controls and axe violations need no model. The model's confusion is the UX signal, so being "a little dumb" is a feature. |
| Personas | Files, caller-chosen by name | Each is a system-prompt fragment plus viewport and pacing. The caller stacks any number. |
| Hints | `QA.md` in the target repo + `--goal` / `--hint` on the command line | Same role as the old goblinchess harness README's "Map of the app". Goal and hints on the CLI stack on top. |
| Verification | Local, deterministic replay of the recorded steps | No paid model for now. A finding whose steps replay to the same state is `confirmed`; otherwise `unreproduced`. |
| Online multiplayer | Skipped | Per decision 2026-09-08. |
| Server startup | URL only | The caller starts the dev server. |

---

## CLI surface

```
waterfree qa run <url> --goal "..." [--hint "..."]... [--persona NAME]... 
                 [--steps N] [--minutes N] [--viewport WxH] [--headed]
                 [--out DIR] [--model ID] [--no-vision] [--workspace PATH]
waterfree qa personas [--workspace PATH]          # built-in + .waterfree/qa/personas/*.md
waterfree qa verify <run-dir>                     # replay each finding's steps
waterfree qa report <run-dir>... [--out FILE]     # consolidated markdown across runs
waterfree qa doctor                               # Playwright, browsers, models present?
```

Conventions follow `docs/cli-surface.md`: JSON on stdout, progress on stderr,
exit `4` when Playwright, a browser, or a model is missing, with the exact
command to fix it.

Output layout mirrors the baseline so existing readers still work:

```
<workspace>/qa-runs/<slug>-<date>/<persona>-<n>/
    log.jsonl          one line per step: snapshot digest, action, result, timing
    shots/NN-label.png
    findings.md        severity-tagged, with repro steps and screenshot refs
    run.json           goal, hints, persona, model, caps, collectors summary
<workspace>/qa-reports/<slug>-<date>.md     from `qa report`
```

---

## The loop

```
snapshot  ->  prompt  ->  model  ->  parse action  ->  execute  ->  record
   ^                                                                  |
   +------------------------------------------------------------------+
```

1. **Snapshot** (`backend/qa/snapshot.py`). Interactive elements only, each
   numbered: role, accessible name or text, test id, state flags
   (`disabled`, `offscreen`, `clipped`, `in-dialog`). Plus visible page text
   trimmed to a budget, open dialogs, URL and title. Between steps only the
   diff is shown when the page is largely unchanged. Budget target: under
   1,500 tokens per turn.
2. **Prompt** (`backend/qa/prompts.py`). System: harness rules, action
   grammar, persona fragment, playbook fragment. User: goal, hints, recent
   history (last 5 actions and their outcomes), current snapshot.
3. **Model** (`backend/qa/driver.py`). One Ollama chat call, low temperature,
   short max tokens, `keep_alive` so the model stays resident between steps.
4. **Parse** (`backend/qa/actions.py`). Strict grammar, one action per turn:

   ```
   CLICK <n>              TYPE <n> "text"        PRESS <key>
   SCROLL up|down         WAIT <seconds<=5>      BACK
   LOOK "question"        NOTE <sev> "title" :: "detail"
   DONE "summary"         STUCK "why"
   ```

   Anything unparseable is fed back once as an error turn, then counts toward
   the stuck budget.
5. **Execute** (`backend/qa/browser.py`). Playwright, muted Chromium, fresh
   profile, reduced motion on by default. `click` uses the actionability
   lessons already in the knowledge base (force-click only after a validated
   target; never build URLs from shell args). Never throws on a missing
   target; it reports `target gone` to the model.
6. **Record** (`backend/qa/findings.py`). Screenshot after every action, JSON
   log line, findings appended with the step index so `verify` can replay.

**Stops:** step cap, wall-clock cap, `DONE`, `STUCK`, or loop detection
(same action on the same snapshot digest three times).

**Collectors** run alongside and never involve the model: console errors,
`pageerror`, failed requests and 4xx/5xx, controls flagged offscreen or
clipped in a snapshot, axe-core violations at each new URL.

**Vision check** (`backend/qa/vision_check.py`): every `--vision-every` steps
and on every `NOTE`, send the screenshot to the large vision tier with the
DOM's one-line claim ("a dialog titled X is open over a chess board") and ask
for agree / disagree / unsure. Disagreements become `note`-severity findings
with both artefacts attached.

---

## Personas

Built-in set in `backend/qa/personas/*.md`; per-workspace additions in
`.waterfree/qa/personas/*.md`. Front matter carries viewport, pacing and
tags; the body is the prompt fragment.

| Name | Traits |
|---|---|
| `first-timer` | Reads nothing, clicks the biggest button, expects the game to teach them |
| `impatient` | Never waits, double-clicks, skips every dialog, hammers Next |
| `careful` | Reads all copy, follows instructions literally, reports every mismatch |
| `keyboard-only` | Tab, Enter, Space, Escape, arrows. Mouse forbidden |
| `phone` | 390x844, tap targets, scroll cues, clipped strips |
| `rules-lawyer` | Tries to break the rules: illegal moves, double spend, undo abuse |

The caller picks any subset. No persona means `first-timer`.

---

## Hints and playbooks

`QA.md` in the target repo is the per-game map: entry points, screen names,
gotchas, a known-good opening, anything the old harness README carried. It is
read verbatim into the prompt, so it should stay under about 150 lines.

Strategy playbooks live in the knowledge base tagged `qa-playbook` and a genre
(`turn-based-board`, `card-game`, `party-lobby`, `puzzle`). `--hint genre:card-game`
pulls the matching playbook. Playbooks list what to try and what to expect:
end-turn spam, resource overflow, undo abuse, save and reload mid-turn,
whether the AI opponent ever acts, whether a win or loss is reachable.

---

## Module layout

```
backend/qa/
├── __init__.py
├── models.py          driver model tiers, env overrides, ensure_available()
├── personas.py        registry, front-matter parser, built-in files
├── personas/*.md
├── hints.py           QA.md loader, playbook lookup via knowledge store
├── snapshot.py        DOM -> numbered element list, text, flags, digest, diff
├── actions.py         grammar, parser, Action dataclass
├── prompts.py         system and turn prompt builders
├── browser.py         Playwright session, execute(action), collectors
├── vision_check.py    screenshot vs DOM claim
├── findings.py        Finding, RunLog, findings.md writer
├── driver.py          the loop, caps, loop detection
├── verify.py          replay recorded steps, mark confirmed / unreproduced
└── report.py          consolidated markdown across run dirs
backend/cli/qa.py      the `qa` area
skills/waterfree-qa/SKILL.md
backend/tests/test_qa_*.py
```

Playwright is imported lazily inside `browser.py` so `waterfree qa personas`
and `qa report` work without it, and the frozen executable does not pay for
it at startup. Missing Playwright or browser exits `4` with the install
commands, mirroring how vision handles a missing model.

---

## Phases

- **Phase 1 — DOM harness.** Snapshot, actions, browser, driver loop,
  collectors, findings, `qa run`, `qa personas`, `qa doctor`, two personas,
  smoke against goblinchess `to_dom`.
- **Phase 2 — Judgement.** Vision check, remaining personas, `QA.md` hints,
  playbooks in the knowledge base, `qa verify`, `qa report`, skill file,
  installer wiring.
- **Phase 3 — Canvas fallback.** When a snapshot has no interactive elements,
  switch to vision-described regions and `CLICK AT x y`. Slower and weaker;
  Paradoxia is the test target. Flagged: expect low completion rates here.

---

## Known risks

- A 9B model will finish simple flows most of the time and lose the thread on
  deep ones. Caps and loop detection bound the cost; `STUCK` is a valid,
  reportable outcome.
- False positives. The baseline's verification table is the reason `verify`
  exists and why every finding must carry replayable steps.
- Playwright inside the PyInstaller build. Browser binaries are not bundled;
  `qa doctor` must make the fix obvious.
