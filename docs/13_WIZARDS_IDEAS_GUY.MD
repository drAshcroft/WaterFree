# Bring Idea to Life Wizard Rebuild

## Summary
- Replace the current fake wizard behavior, where `startWizard` just calls `startSession(goal)`, with a real markdown-first workflow for `Bring Idea to Life`.
- Launching the wizard opens a living markdown document under `.waterfree/wizards/<run-id>/bring-idea-to-life.md`; this becomes the user-facing source of truth for idea refinement, approved outputs, and next-step handoff.
- Step progression is explicit and gated. Each step is run, reviewed, optionally revised, and accepted before the next step can run.
- Accepted outputs feed the existing session and backlog engine. The wizard does not create a parallel coding system; it seeds the current `PlanDocument`, `tasks.db`, annotations, and execution queue.

## Key Changes
**UI and editor flow**
- Keep the sidebar wizard card only as a launcher/resume entry point. Remove the current textarea-as-wizard behavior for `Bring Idea to Life`.
- Opening the wizard creates or resumes a markdown doc and focuses it in the editor.
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
- `BDD AI Tests` uses a new `bdd_test_designer` persona/subagent and generates human-language acceptance scenarios plus test-oriented tasks tied to the micro prompts.
- `Coding Agents` uses a new `coding_agent` persona/subagent only to convert accepted micro prompts and BDD tasks into executable session tasks. It does not re-plan from the raw idea.
- `Review` uses a new `reviewer` persona/subagent and aggregates unresolved issues, blocked procedures, skipped items, diagnostics, and follow-up design work back into backlog tasks for the earlier agents.
  

## Public Interfaces
- Replace sidebar action semantics for `Bring Idea to Life` from `startWizard(goal)` to `openWizard(wizardId, goal, persona)`.
- Add extension commands for wizard lifecycle: `openWizard`, `runWizardStep`, `acceptWizardStep`, `reviseWizardStep`, `promoteWizardTodos`, `startWizardCoding`, and `runWizardReview`.
- Add bridge/server RPCs: `createWizardSession`, `getWizardSession`, `runWizardStep`, `acceptWizardStep`, `startWizardCoding`, and `runWizardReview`.
- Add new backend types for `WizardRun`, `WizardStepState`, `WizardStepResult`, and `WizardTodoExport`.
- Extend persona/subagent registries with `market_researcher`, `bdd_test_designer`, `coding_agent`, and `reviewer`.
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
