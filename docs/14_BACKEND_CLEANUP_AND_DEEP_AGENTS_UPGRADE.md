# Subsystem 14 — Backend Cleanup And Deep Agents Upgrade
## WaterFree VS Code Extension

---

## Purpose

WaterFree adopted Deep Agents late enough that the backend now contains three overlapping ideas:

1. WaterFree-owned protocol state and workflow
2. A provider-neutral runtime boundary
3. Leftover Claude-era assumptions and tests

This document defines the cleanup plan and the upgrade path. The goal is not to move all backend logic into Deep Agents. The goal is to:

- keep WaterFree's negotiated workflow as the product
- remove stale provider-specific scaffolding
- let Deep Agents handle the orchestration work it is actually good at
- define how personas should use any new tools or subagent capabilities Deep Agents makes available

---

## Current Assessment

### What should remain WaterFree-owned

These parts are product logic, not runtime implementation details:

- session persistence under `.waterfree/sessions/`
- plan, task, annotation, and approval state
- wizard run state and stage acceptance
- task store and workspace backlog ownership
- graph, knowledge, debug, and todo MCP/server integration
- context lifecycle and project memory management
- extension-to-backend transport over stdio JSON-RPC

### What is currently over-built or duplicated

The backend still carries code that assumes the old direct-provider model:

- `_claude` compatibility aliasing in `backend/server.py`
- Claude-era comments and naming in execution flows
- Anthropic-specific tool schema adapters in the shared tool catalog
- dead tests that still import `backend.llm.claude_client`
- knowledge extraction code that bypasses the runtime and calls Anthropic directly

### What Deep Agents should do better

These are the areas where built-in Deep Agents behavior is a better fit than local hand-rolled orchestration:

- tool orchestration within a turn
- subagent spawning and delegation
- checkpoint/interrupt integration around tool calls
- structured execution with per-agent tool limits
- filesystem-backed coding lanes when policy allows it

### Bottom-line split

Roughly:

- 50-60% of the backend should stay WaterFree-specific
- 25-35% should move closer to built-in Deep Agents orchestration
- 10-20% is cleanup debt from the pre-Deep-Agents design

---

## Target Architecture

```text
VS Code Extension
  -> PythonBridge
  -> backend/server.py
      -> WaterFree protocol layer
         - sessions
         - annotations
         - approvals
         - wizard state
         - backlog ownership
      -> AgentRuntime boundary
         - planning lane
         - annotation lane
         - execution lane
         - debug lane
         - knowledge lane
      -> Deep Agents implementation
         - tool orchestration
         - subagents
         - interrupts/checkpoints
         - filesystem backend
      -> WaterFree tool registry + skills adapter
         - graph
         - todos
         - knowledge
         - debug
         - optional web/docs tools
```

### Rule

Deep Agents owns agent execution behavior. WaterFree owns durable truth.

That means:

- Deep Agents may decide which allowed tool to call next
- Deep Agents may delegate to a bounded subagent
- Deep Agents may request interrupts/checkpoints
- WaterFree still owns session status, approved intent, task completion, wizard transitions, and persisted state

---

## Cleanup Plan

### Phase 1 — Remove stale Claude-era surface

#### Goals

- stop carrying broken imports and dead naming
- make the runtime API describe the system that actually exists

#### Work

- remove `_claude` aliasing from `backend/server.py`
- rename stale comments that still describe runtime calls as "Claude"
- delete or replace tests that import `backend.llm.claude_client`
- remove Anthropic-only tool conversion helpers from shared tool descriptors
- remove any runtime fallbacks that only exist to preserve deleted provider code paths

#### Exit criteria

- no backend code refers to `ClaudeClient`
- no tests import `backend.llm.claude_client`
- shared tool types are provider-neutral

---

### Phase 2 — Fix the runtime contract

#### Problem

`AgentRuntime` still exposes methods that the current Deep Agents runtime does not cleanly implement, while the server depends on them.

#### Work

- decide which methods remain part of the stable runtime contract:
  - `generate_plan`
  - `generate_annotation`
  - `execute_task`
  - `run_wizard_stage`
  - `checkpoint`
  - `resume`
- either implement or remove these legacy methods:
  - `detect_ripple`
  - `alter_annotation`
  - `analyze_debug_context`
  - `answer_question`
- split "protocol actions" from "runtime actions"

#### Recommendation

Keep the runtime contract narrow. Prefer:

- protocol/controller layer handles redirects, approval state, session notes, and persistence
- runtime handles agent reasoning, tool orchestration, code-edit planning, debug analysis, and structured outputs

#### Exit criteria

- every method in `AgentRuntime` is implemented by the default runtime
- `server.py` does not call optional runtime methods without a clear fallback

---

### Phase 3 — Move from prompt-wrapper mode to real Deep Agents orchestration

#### Problem

The current runtime mostly asks for JSON-only responses and parses the text back. That uses Deep Agents as a fancy provider adapter rather than as an orchestration framework.

#### Work

- replace "Return JSON only" prompt patterns with structured tool-backed flows where practical
- let the runtime use allowed tools during planning and execution instead of relying on giant prebuilt context strings alone
- move subagent use from static metadata to actual agent delegation
- wire interrupt handling to WaterFree checkpoints

#### Recommendation

Use a hybrid approach:

- keep WaterFree-generated context summaries because they encode product-specific state well
- let Deep Agents perform tool-driven follow-up work inside the turn
- require structured final outputs for plan, annotation, wizard payloads, and execution artifacts

#### Exit criteria

- planning can query graph/task/knowledge tools through the runtime
- execution can use filesystem or patch tools under policy
- subagent delegation performs real bounded work instead of writing checkpoint placeholders only

---

### Phase 4 — Move knowledge workloads behind the runtime

#### Problem

Knowledge extraction and procedure extraction still call Anthropic directly, which bypasses runtime selection and prevents local-lane routing.

#### Work

- introduce a knowledge/extraction lane under the runtime boundary
- allow routing to:
  - local Ollama for bulk classification and triage
  - remote model fallback for difficult synthesis
- keep `KnowledgeStore` and extraction pipelines WaterFree-owned

#### Exit criteria

- no backend knowledge extraction path constructs `anthropic.Anthropic` directly
- extraction workloads can be routed independently from planning/execution workloads

---

### Phase 5 — Rationalize filesystem execution

#### Current choice

The backend currently returns edit payloads for the extension to apply.

#### Options

Option A: keep editor-applied edits

- preserves strong editor-side visibility
- fits WaterFree's negotiated workflow
- keeps backend from mutating source directly

Option B: allow Deep Agents filesystem backend for coding lanes

- gives more natural tool-driven execution
- simplifies some multi-file operations
- needs stronger checkpointing and touched-file reporting

#### Recommendation

Use both, but by lane:

- architect/research personas remain read-heavy and doc-writing only
- coding personas may use filesystem tools in workspace sandbox
- final source-of-truth edit summary must still be surfaced back through WaterFree protocol state

#### Exit criteria

- every write-capable tool call is checkpoint-aware
- touched files are recorded in checkpoint payloads
- read-only personas cannot mutate source files

---

## Persona Upgrade Plan

Deep Agents will make more tools available than the original backend assumed. WaterFree personas need explicit tool budgets and role expectations so the agent layer does not become noisy or unsafe.

### Shared persona rules

All personas must follow these rules:

- never own persistent session truth
- only use tools explicitly allowed for the persona
- request checkpoints before networked or destructive actions
- return structured outputs that map back into WaterFree documents, annotations, tasks, or notes
- avoid long autonomous loops

---

### Architect

#### Role

System decomposition, trade-offs, phased rollout, subsystem shaping.

#### Current tools

- graph
- knowledge
- tasks

#### New Deep Agents tools to allow

- subagent delegation
- optional docs retrieval
- optional web research
- doc-writing filesystem access for `docs/` and `.waterfree/`

#### Rules

- may create plans, subsystem proposals, ADR suggestions, and wizard stage drafts
- may not edit application source by default
- may delegate to `pattern_expert` or `market_researcher`
- must checkpoint before any web/networked research

---

### Pattern Expert

#### Role

Pattern matching, framework conventions, reuse guidance, design guardrails.

#### New Deep Agents tools to allow

- graph
- knowledge
- optional docs retrieval
- subagent-local structured analysis tools

#### Rules

- read-only by default
- returns reference patterns, caveats, and recommended implementation shapes
- may not directly write source files
- should be the first delegate for "how should this be shaped?" questions

---

### Debug Detective

#### Role

Paused-state analysis, root cause discovery, reproduction narrowing.

#### New Deep Agents tools to allow

- debug snapshot tools
- graph
- knowledge
- task creation suggestions
- optional docs retrieval

#### Rules

- read-only during diagnosis
- may emit follow-up tasks, annotations, or probable root-cause reports
- should not write source until a separate coding lane is approved

---

### Stub Wireframer

#### Role

Scaffolding, shell structure, interface stubs, prompt-to-code handoff.

#### New Deep Agents tools to allow

- graph
- task store
- filesystem sandbox
- patch-oriented write tools

#### Rules

- may create non-destructive scaffolding when explicitly approved
- should prefer patch-oriented edits over free-form writes
- should operate with narrow file allowlists

---

### Market Researcher

#### Role

Idea validation, comparable products, audience framing, differentiation.

#### New Deep Agents tools to allow

- optional web search
- optional docs retrieval
- doc-writing access for research artifacts

#### Rules

- no source-code writes
- every networked research pass requires checkpoint approval
- output should land in wizard docs and structured market-research sections

---

### BDD Test Designer

#### Role

Acceptance criteria, scenario design, human-readable testing plans.

#### New Deep Agents tools to allow

- task store
- graph
- optional test-file write tools

#### Rules

- default mode is read-only plus task/test-plan generation
- test-file generation may be enabled in approved execution lanes
- should not mutate production source files

---

### Coding Agent

#### Role

Implements approved work items.

#### New Deep Agents tools to allow

- graph
- task store
- filesystem sandbox
- patch-oriented write tools
- test invocation tools when added

#### Rules

- only runs after annotation approval or equivalent checkpointed coding handoff
- must emit touched files, rationale, and unresolved risks
- may delegate narrowly to `pattern_expert` or `reviewer`

---

### Reviewer

#### Role

Code review, issue finding, regression detection, follow-up task generation.

#### New Deep Agents tools to allow

- graph
- task store
- diagnostics/test-result tools
- read-only filesystem tools

#### Rules

- read-only
- findings-first output
- may create follow-up tasks but not apply fixes unless re-routed into coding mode

---

## Tool Policy Matrix

| Tool category | Architect | Pattern Expert | Debug Detective | Stub Wireframer | Market Researcher | BDD Test Designer | Coding Agent | Reviewer |
|---|---|---|---|---|---|---|---|---|
| Graph/search tools | Yes | Yes | Yes | Yes | Limited | Yes | Yes | Yes |
| Knowledge tools | Yes | Yes | Yes | Limited | Limited | Limited | Yes | Yes |
| Todo/task tools | Yes | Limited | Yes | Yes | No | Yes | Yes | Yes |
| Debug tools | No | No | Yes | No | No | No | Limited | Limited |
| Filesystem read | Yes | Yes | Yes | Yes | Limited | Yes | Yes | Yes |
| Filesystem write docs | Yes | No | No | Limited | Yes | Limited | Limited | No |
| Filesystem write source | No | No | No | Yes | No | Limited | Yes | No |
| Web/docs retrieval | Optional | Optional | Optional | No | Yes | Optional | No | Optional |
| Subagent delegation | Yes | No | Limited | No | No | No | Limited | Limited |
| Test run tools | No | No | Limited | No | No | Yes | Yes | Yes |

### Notes

- "Limited" means persona-specific allowlists, file constraints, or explicit stage gating.
- Networked tools remain disabled by default.
- Write-capable tools require checkpoint policy integration.

---

## Upgrade Sequence

### Milestone 1 — Backend cleanup

- remove stale Claude naming and imports
- replace dead tests
- shrink the runtime interface to supported behaviors

### Milestone 2 — Real Deep Agents tool orchestration

- planning and execution use allowed tools through the runtime
- checkpoint interrupts are wired to WaterFree approval state

### Milestone 3 — Real subagents

- architect delegates to pattern/research/debug specialists
- outputs return as structured artifacts into session or wizard state

### Milestone 4 — Knowledge lane routing

- move extraction workloads behind the runtime
- support Ollama-first bulk processing

### Milestone 5 — Persona-specific filesystem and test tooling

- enable coding lanes to patch source
- keep research/review personas read-only

---

## Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| Runtime bloat | Too much WaterFree logic gets stuffed into Deep Agents prompts | keep protocol state outside the runtime |
| Tool sprawl | More tools can reduce focus and increase bad calls | strict persona allowlists |
| State divergence | Checkpoint state and session state can disagree | WaterFree session remains authoritative |
| Over-delegation | Subagents can create noisy side quests | narrow scopes, structured outputs, no open-ended loops |
| Unsafe writes | Filesystem-backed coding can become too broad | workspace sandbox, patch orientation, touched-file reporting |

---

## Definition Of Done

The upgrade is complete when:

- the backend has no stale Claude-only runtime surface
- the default runtime fully satisfies the runtime contract
- Deep Agents performs real tool orchestration rather than only JSON-text wrapping
- at least architect, coding, reviewer, and debug personas have explicit tool policies implemented
- knowledge extraction is routed through the runtime boundary
- checkpoint and interrupt behavior is consistent across tool categories

---

## Recommended First Sprint

1. Remove broken Claude-era tests and replace them with runtime-level tests.
2. Clean `AgentRuntime` and `server.py` so every required method exists and is used consistently.
3. Remove Anthropic-only tool descriptor helpers from shared tool types.
4. Implement real subagent delegation for architect and reviewer lanes.
5. Move knowledge extraction onto a runtime-selected lane.

---

## Final Rule

WaterFree should not try to out-orchestrate Deep Agents, and Deep Agents should not try to own WaterFree's protocol.

The clean split is:

- WaterFree owns truth, workflow, and safety policy
- Deep Agents owns bounded agent execution inside that policy
