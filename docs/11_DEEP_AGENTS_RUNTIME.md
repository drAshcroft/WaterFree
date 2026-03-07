# Subsystem 11 — Deep Agents Runtime
## WaterFree VS Code Extension

---

## Purpose

WaterFree needs a provider-neutral agent runtime before the project grows into a pile of one-off LLM adapters. The goal of this subsystem is to make room for:

- Many LLM providers, not just Anthropic
- Local Ollama models for deep snippetization and knowledge extraction without token burn
- First-class skills, not prompt fragments hidden in code
- Subagents with clear roles and bounded authority
- Filesystem harnessing with policy and approval gates
- Checkpointed human-in-the-loop before risky or expensive actions
- Optional MCP servers for web search, retrieval, docs, and future external tools

This is not a license to throw away the current negotiated WaterFree workflow. The protocol remains the product. Deep Agents becomes the runtime layer underneath it.

---

## Why Deep Agents

Deep Agents is a better fit than a thin provider wrapper when WaterFree needs all of the following at once:

1. **Skills as a first-class primitive**
2. **Subagents with scoped tools**
3. **Filesystem tools with harnessing**
4. **Checkpoint and interrupt support for human review**
5. **MCP-aware tool loading**

If WaterFree only needed model switching, a smaller abstraction would be enough. The reason to adopt Deep Agents is the combination of skills, tool orchestration, and human checkpoints.

---

## Architectural Decision

Adopt a provider-neutral runtime boundary in `backend/llm/` and make Deep Agents an implementation of that boundary, not the boundary itself.

**Do:**
- Keep the VS Code extension and `PythonBridge` transport stable
- Keep WaterFree session state, annotations, approvals, and backlog ownership as the source of truth
- Add a runtime interface that can be backed by Anthropic-native code, Deep Agents, or future providers

**Do not:**
- Hard-wire the whole product to one orchestration framework
- Let Deep Agents own persistent session truth directly
- Make open-web tools mandatory for every persona or every workspace

---

## Proposed Runtime Shape

```text
backend/llm/
├── runtime.py                 # AgentRuntime protocol
├── runtime_registry.py        # choose runtime per task/stage/persona
├── providers/
│   ├── anthropic_runtime.py   # current direct tool loop
│   ├── deep_agents_runtime.py # LangChain Deep Agents backend
│   ├── ollama_runtime.py      # local chat / extraction / classification lane
│   └── openai_runtime.py      # future
├── tools/
│   ├── registry.py            # canonical tool catalog and policy metadata
│   ├── graph_tools.py
│   ├── task_tools.py
│   ├── knowledge_tools.py
│   ├── filesystem_tools.py
│   └── web_tools.py           # optional MCP-backed adapters
├── skills/
│   ├── loader.py              # reads skills/*/SKILL.md
│   ├── registry.py            # resolves skills by persona/stage/task
│   └── adapters.py            # projects WaterFree skills into Deep Agents
└── checkpoints/
    ├── store.py               # durable checkpoint state
    └── policies.py            # approval gates
```

The existing `ClaudeClient` becomes one runtime implementation rather than the permanent center of gravity.

---

## Runtime Interface

```python
class AgentRuntime(Protocol):
    def generate_plan(self, goal: str, context: str, *, persona: str) -> tuple[list[Task], list[str]]: ...
    def generate_annotation(self, task: Task, context: str, *, persona: str) -> IntentAnnotation: ...
    def execute_task(self, task: Task, context: str, *, persona: str) -> list[dict]: ...
    def answer_question(self, question: str, context: str, *, persona: str) -> dict: ...
    def checkpoint(self, session_id: str, reason: str, payload: dict) -> dict: ...
    def resume(self, checkpoint_id: str, decision: dict) -> dict: ...
```

WaterFree should select a runtime by stage and workload, not by ideology.

---

## Model Routing Strategy

Not every model should do every job.

| Workload | Preferred lane | Why |
|---|---|---|
| Session planning, architecture, trade-off analysis | Strong remote model | Better reasoning and synthesis |
| Intent annotation and negotiated edits | Strong remote model | Better tool use and instruction fidelity |
| Workspace snippet triage | Local Ollama model | Cheap, parallel, repeatable |
| Deep procedure extraction and knowledge building | Local Ollama model first, remote fallback | Avoid token burn on bulk analysis |
| Embeddings / reranking / semantic retrieval | Local models where possible | Cost and privacy |
| Web research and external comparisons | Remote model plus optional search MCP | Source-aware retrieval |

**Ollama is a lane, not the whole runtime.** WaterFree should be able to route knowledge extraction to Ollama without forcing plan execution onto a local model that cannot handle it well.

---

## Skills

WaterFree already has a local `skills/` directory and a `SKILL.md` convention. That should remain the canonical skills format.

### Required skill behavior

- Skills are selected by persona, stage, and task type
- Skills can attach references, scripts, and policy notes
- Skills can contribute tool policies, not just prompt text
- Skills can be loaded by subagents without loading every skill into every prompt

### Rule

Do not invent a second, incompatible skill system just because Deep Agents has one. Project WaterFree skills into the runtime through an adapter layer.

---

## Subagents

The likely first subagents are already visible in WaterFree's persona model:

- `architect`
- `pattern_expert`
- `stub_wireframer`
- `debug_detective`

### Subagent policy

- Subagents get narrow tool sets
- Subagents return structured outputs, not free-form side quests
- The parent agent owns final handoff to the WaterFree session
- Long-lived autonomous loops are off by default

### Example split

| Subagent | Allowed tools |
|---|---|
| Architect | graph, knowledge, tasks, optional web search |
| Pattern expert | graph, knowledge, tasks, optional docs retrieval |
| Stub/Wireframes | graph, tasks, filesystem harness |
| Debug detective | debug MCP, graph, knowledge |

---

## Filesystem Harnessing

Filesystem power is necessary, but broad unrestricted writes are a bad default.

### Required guardrails

- Workspace-root sandbox only
- Patch-oriented edits preferred over free-form writes
- File creation and deletion surfaced in the checkpoint payload
- Path allowlists for generated artifacts
- Read-only mode for research personas

### Practical rule

The architect should be able to read broadly, write docs and plan artifacts, and queue work. The stub and execution lanes should be the ones allowed to change source files.

---

## Checkpointed Human-in-the-Loop

Deep Agents should plug into WaterFree's existing approval model, not replace it.

### Required checkpoints

1. Before final plan acceptance
2. Before annotation approval turns into file edits
3. Before any networked or paid tool call above policy threshold
4. Before destructive filesystem actions
5. Before task completion when editor diagnostics show blocking errors

### Storage

Checkpoint state should live under `.waterfree/sessions/` beside session documents so recovery after editor restart is straightforward.

---

## MCP Strategy

Web search and retrieval should live as optional MCP servers, not as hard-coded product behavior.

### Core MCPs

- `waterfree-index`
- `waterfree-todos`
- `waterfree-knowledge`
- `waterfree-debug`

### Optional MCPs

- `waterfree-web` or external web-search MCP
- `waterfree-retrieval` for docs, PDFs, or site mirrors
- external issue tracker MCPs
- external design-system MCPs

### Rule

Optional MCPs must be discoverable, permissioned, and disabled by default. Personas may prefer them, but the system must function without them.

---

## Rollout Plan

### Phase 1 — Runtime boundary

- Introduce `AgentRuntime`
- Move current Anthropic logic behind the boundary
- Keep behavior identical

### Phase 2 — Tool registry

- Centralize graph, task, knowledge, and filesystem tool definitions
- Add tool metadata: cost, network access, write access, approval required

### Phase 3 — Skills adapter

- Load local `skills/*/SKILL.md`
- Attach references and scripts through a runtime adapter

### Phase 4 — Ollama lane

- Add local chat and embedding support for knowledge extraction workloads
- Pilot local snippet triage before broader usage

### Phase 5 — Deep Agents runtime

- Add Deep Agents for architect research, skill-aware orchestration, and subagents
- Keep the negotiated WaterFree protocol above it

### Phase 6 — Optional MCP web search

- Add external research only as an explicit MCP capability
- Gate network usage by policy and checkpoint

---

## Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| Framework lock-in | Deep Agents may not remain the best backend forever | keep `AgentRuntime` narrow and owned by WaterFree |
| Tool sprawl | Too many tools produce noisy agent behavior | per-persona tool allowlists and approval policy |
| Prompt dilution | too many loaded skills and docs reduce focus | progressive disclosure and skill filtering |
| Local model quality drift | Ollama models vary in quality by task | use local-first only for the right workloads |
| State confusion | framework checkpoints and WaterFree sessions can diverge | WaterFree session remains authoritative |

---

## Bottom Line

Deep Agents is worth adopting if WaterFree wants a real multi-provider, skill-aware, subagent-capable runtime. It is not worth adopting as a cosmetic wrapper around the current Anthropic client.

The correct move is:

1. Own the runtime boundary
2. Keep skills canonical in WaterFree
3. Add Ollama for bulk local knowledge work
4. Treat web search and retrieval as optional MCP capabilities
5. Preserve WaterFree's checkpointed collaboration protocol as the product
