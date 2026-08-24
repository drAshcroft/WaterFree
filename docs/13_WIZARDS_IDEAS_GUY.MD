# Bring Idea to Life Wizard Rebuild

## Summary
- Replace the current fake wizard behavior, where `startWizard` just calls `startSession(goal)`, with a real markdown-first workflow for `Bring Idea to Life`.
- Launching the wizard opens a living markdown document under `.waterfree/wizards/<run-id>/bring-idea-to-life.md`; this becomes the user-facing source of truth for idea refinement, approved outputs, and next-step handoff.
- Step progression is explicit and gated. Each step is run, reviewed, optionally revised, and accepted before the next step can run.
- Accepted outputs feed the existing session and backlog engine. The wizard does not create a parallel coding system; it seeds the current `PlanDocument`, `tasks.db`, annotations, and execution queue.

## Key Changes
**UI and editor flow**
- Keep the sidebar wizard card only as a launcher/resume entry point. Remove the current textarea-as-wizard behavior for `Bring Idea to Life` so the card is just a launch button.
- Opening the wizard creates or resumes a markdown doc in the public docs folder and focuses it in the editor.
- A new market-research doc opens as a plain prompt, `What is your idea? (describe in detail)`, with a single `Send for Refinement` action instead of internal wizard status blocks.
- Make the markdown interactive with a `CodeLensProvider` and editor title actions for wizard docs. The available actions are `Accept Step` and `Revise Document` .
- The markdown file uses fixed managed markers so the extension can safely rewrite generated blocks without clobbering human edits.
- The document structure is depends on step: 

Market research has chunks:  `Initial Goal`, `Similar Ideas and niches` (What is good, what is bad, is this idea different),`Who wants this?`, `Core feature`, `Minimum viable product`, `Feature list`, `Stretch goals`...
Architect has chunks:`Comparable Architectures`,`Tech Stack`,`System Constraints`,`Data Model and Storage`,`Security Concerns`,`Privacy and Policy`,`Scalability Targets`,`Deployment Model`,`Top Level Architecture`,`Development Roadmap`,`Reality Check ("Are you kidding?")`

Design will have a single page for each subsystem
Design has chunks: `Similar Subsystems`, `3rd party APIs`,`Subsystem Overview`, `Communication Patterns`, `Where do the subsystem go`, `Interfaces`,`Data Contracts`, `Logging`, `Failure Handling`, `Subsystems todos`

  
- In each chunk, the AI generated text can be refined, or questioned by the user. `Accepted Chunk` will lock that section down allowing other parts of the document to be changed with better knowledge from the agent

**Wizard state and persistence**
- Add a new wizard run model persisted beside the markdown doc as `.waterfree/wizards/<run-id>/wizard.json`.
- `wizard.json` stores: wizard type, goal, current step, step statuses, accepted summaries, derived task ids, linked `sessionId`, created/updated timestamps, and the artifact lineage needed to resume.
- The markdown doc is the readable artifact; `wizard.json` is the machine state. On resume, the extension reloads `wizard.json` and re-renders CodeLens/actions against the markdown file.
- Use a single living document for market research, one for architect, but the design agent will produce step files for sub systems. The stubs and reviewer will produce code files

**Step engine and agent mapping**
- Add a dedicated step pipeline for `Bring Idea to Life` with fixed roles and outputs:
- `Market Research` uses a new `market_researcher` persona/subagent and produces idea framing, “why this is cool”, audience, differentiators, risks, and a refined mega prompt.
- `Architect Review` uses `architect` and produces requirements, stack recommendations, roadmap, high-level design, open questions, and macro todos.
- `Design Pattern Agent` uses `pattern_expert` and turns the architecture into subsystem boundaries, interfaces, data-flow policy, pattern choices, anti-pattern warnings, and subsystem todos.
- `Wireframe Agents` uses `stub_wireframer` and turns subsystem todos into file/interface skeleton plans, pseudocode, stubs, and accepted micro prompts.
- `Design Audit` uses the `design_auditor` persona/subagent and cross-checks the accepted architecture, subsystem designs, and wireframes against each other for contradictions, unencodable "enforceable" rules, underspecified points, and requirements the design needs but never states. It gates `BDD AI Tests`: this is the last step where a disagreement between design documents is a document edit rather than a defect found during implementation. Distinct from `Review`, which looks backwards at executed work.
- `BDD AI Tests` uses a new `bdd_test_designer` persona/subagent and generates human-language acceptance scenarios plus test-oriented tasks tied to the micro prompts.
- `Coding Agents` uses a new `coding_agent` persona/subagent only to convert accepted micro prompts and BDD tasks into executable session tasks. It does not re-plan from the raw idea.
- `Review` uses a new `reviewer` persona/subagent and aggregates unresolved issues, blocked procedures, skipped items, diagnostics, and follow-up design work back into backlog tasks for the earlier agents.
  

## Public Interfaces
- Replace sidebar action semantics for `Bring Idea to Life` from `startWizard(goal)` to `openWizard(wizardId, goal, persona)`.
- Add extension commands for wizard lifecycle: `openWizard`, `runWizardStep`, `acceptWizardStep`, `reviseWizardStep`, `promoteWizardTodos`, `startWizardCoding`, and `runWizardReview`.
- Add bridge/server RPCs: `createWizardSession`, `getWizardSession`, `runWizardStep`, `acceptWizardStep`, `startWizardCoding`, and `runWizardReview`.
- Add new backend types for `WizardRun`, `WizardStepState`, `WizardStepResult`, and `WizardTodoExport`.
- Extend persona/subagent registries with `market_researcher`, `bdd_test_designer`, `coding_agent`, `design_auditor`, and `reviewer`.
- Keep existing `createSession`, `generateAnnotation`, `executeTask`, `listTasks`, and `queueTodoInstruction` contracts intact.

## Test Plan
- Build tests

## Assumptions
- Only `Bring Idea to Life` is rebuilt in this pass. Other wizard cards  are clearly marked as not yet upgraded.
- Wizard artifacts live under `.waterfree` and are gitignored by default.
- The wizard is markdown-first. Interactivity is provided by CodeLens/editor actions, not by another sidebar mini-app.
- Step progression always requires explicit approval.
- Market research is local-first. If optional web-capable runtime tools are unavailable, the step must say that explicitly and continue with local architecture/docs/knowledge only.  If no websearch is available, build a detailed prompt to allow user to get information from chatGPT web
- The living markdown doc is the human source of truth; `wizard.json` exists only to keep state and integration deterministic.

## Launcher and Coding Handoff (2026-08-23)

**Launcher discoverability.** The sidebar wizard card previously rendered the
`Launch Wizard` button only inside the expanded body, behind a disclosure whose
sole affordance was a muted right-hand label (`7 steps`). Ten collapsed rows with
no visible control read as a removed feature. The launch control now lives on the
collapsed row itself:

- Every implemented wizard shows a `Launch` / `Resume` button without any prior
  interaction. A `>`/`v` chevron marks the disclosure separately.
- Wizards with no stage pipeline behind them render a `Soon` badge instead of a
  button. They used to offer an identical `Launch Wizard` that reached the
  backend only to bounce off an information toast.
- The extension pushes a `wizardStatus` message (`PlanSidebarProvider.sendWizardStatus`)
  built from `getWizardSession`, so an in-flight run shows `Resume` plus a
  `<stage> - step N of M` line rather than a fresh `Launch`.

**Coding executor.** `startWizardCoding` used to stop at "session created": it
promoted todos into `tasks.db` and left a human to run each one, which left the
ACP work with no pipeline feeding it. The coding stage now delegates each
agent-owned pending task to an ACP agent (`AcpSubagentDriver.delegate` with
`deliverable: "files"`), sequentially, so concurrent agent processes cannot
conflict in one worktree.

`waterfree.wizardCodingExecutor` selects the executor:

| value | behavior |
| --- | --- |
| `acp` (default) | Send the accepted coding tasks to a configured ACP agent. Warns and falls back to queueing when `waterfree.acpAgents` is empty. |
| `provider` | Leave the tasks pending for the internal runtime. |
| `ask` | Prompt at the start of each coding stage. |

Still open (tracked in the backlog): task status in `tasks.db` is not yet updated
from `DelegationResult`, so `Review` cannot aggregate what the agent actually did.

## Sidebar Settings Was a Dead End on Narrow Widths (2026-08-24)

Opening Settings from the Wizards sidebar could trap you with no way back to the
main view short of restarting the editor. It was a layout bug, not a logic bug --
every handler worked, and the `Close` button was in the DOM the whole time.

`.settings-dialog-header` is a flex row of `Back` / title / `Close`. The title was
`flex: 1` with no `min-width: 0`, so it refused to shrink below its own text, and
two `min-width: 58px` buttons plus gaps gave the header a ~187px floor. Measured
at a 180px sidebar: the header's content box is 138px, `Close` lands at
`x=150..208`, and the panel itself ends at 170. A webview sidebar has no
horizontal scroll, so the button was rendered but unreachable.

`Back` was not an escape either -- it returns to the settings menu, and the menu
renders no `Back` at all (`isMenu` suppresses it), leaving the same off-screen
`Close` as the only exit.

Fixed with two independent guards, so neither alone has to be sufficient:

- the title takes `flex: 1 1 0` + `min-width: 0` and ellipsis-truncates, which
  keeps the row on one line at every realistic width;
- the header may `flex-wrap`, so if the buttons still cannot fit they move to a
  second row instead of overflowing off-screen.

`.sidebar-title` had the identical defect and pushed the `History` button out
below ~120px; it got the same treatment.

**Regression guard.** `scripts/sidebar-layout/check.py` (`npm run test:sidebar`)
renders the real `sidebar.js`/`sidebar.css` headlessly at 340/260/220/180/150/120px
and fails if any interactive control's box escapes the viewport, or if `Close`
is unreachable on any settings page. It was confirmed to fail on the pre-fix CSS
and pass after. DOM-only assertions cannot catch this class of bug -- the element
exists and is `visible`; only its geometry is wrong.

Known and accepted: at 100px the quick-job selects and the MCP `Browse` button
still overflow. VS Code will not drag a sidebar that narrow, and none of them
trap navigation.
