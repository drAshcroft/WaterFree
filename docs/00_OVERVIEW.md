# WaterFree — VS Code Extension
## Master System Overview

---

## Vision

PairProtocol is a VS Code extension that implements structured AI pair programming. It is not an autocomplete tool, a chat assistant, or an autonomous agent. It is a **turn-based, negotiated collaboration protocol** between a human developer and an AI model, modelled on how two experienced developers actually work together.

The guiding principle: **the AI explains its intent before it acts, and the human decides whether it proceeds.**

The experience should feel like working with an obsessive, highly competent colleague who:
- Has read the entire codebase before touching anything
- Writes margin notes explaining what they want to do and why
- Waits for your sign-off before writing a line
- Checks every side effect, style violation, and error implication
- Updates their understanding when you change direction
- Never interrupts you while you're thinking

---

## Core Problems Being Solved

| Problem with current tools | PairProtocol solution |
|---|---|
| AI acts without shared context | Planning phase establishes shared mental model before any code is written |
| Suggestions interrupt flow | AI is silent unless invoked or it's their "turn" |
| AI doesn't understand the codebase | Full indexing with Tree-sitter + semantic vector index before session starts |
| No way to negotiate intent | Intent Annotation Layer — AI writes what it *plans* before it does it |
| Todos and redirects get lost | Inline TODO comments are treated as live instructions |
| Side effects are missed | Persistent code graph, re-scanned on every save |
| AI forgets context mid-session | Session state is serialized and restored; the plan document is the single source of truth |

---

## System Architecture — High Level

```
VS Code Extension Host (TypeScript — thin UI shell)
  ├── Registers commands, TreeViews, CodeLens, FileWatcher, DebugAdapterTracker
  ├── Renders UI: Plan sidebar, Quick Actions sidebar, status bar, annotation overlays
  └── Communicates with Python backend via stdin/stdout JSON-RPC
           │
           │  Newline-delimited JSON over stdio (no ports, no network)
           ▼
Python Backend Process (spawned by extension on activate)
  ├── indexer/      — tree-sitter parsing, code graph, file watching
  ├── llm/          — Anthropic SDK, prompt templates, context builder
  ├── session/      — PlanDocument, task queue, session persistence
  ├── negotiation/  — TurnManager (8-state machine), NegotiationController
  └── debug/        — DebugContext model, live debug formatting
           │
           ▼
  .waterfree/  (workspace-local, gitignored)
  ├── index.json, graph.json
  └── sessions/current.json
```

**Why stdio JSON-RPC over HTTP:** no port conflicts, process lifecycle is coupled (extension kills backend on deactivate), works in VS Code remote environments, no CORS issues.

---

## Subsystem Index

| File | Subsystem | Purpose |
|---|---|---|
| `01_CODEBASE_INDEXING.md` | Codebase Index | Tree-sitter parsing, vector embeddings, code graph |
| `02_PLANNING_PROTOCOL.md` | Session Manager + Planning | Shared goal doc, task list, P0 navigation |
| `03_INTENT_ANNOTATION.md` | Intent Annotation Layer | Shadow text, accordion comments, pre-action declarations |
| `04_NEGOTIATION_PROTOCOL.md` | Negotiation Protocol | Approve/Alter/Redirect, inline TODOs, turn management |
| `05_SIDE_EFFECT_WATCHER.md` | Side Effect Watcher | Ripple detection, style guide enforcement, error interpretation |
| `06_EXTENSION_SCAFFOLD.md` | VS Code Extension Shell | Extension structure, APIs used, build system, entry points |
| `07_LIVE_PAIR_DEBUG.md` | Live Pair Debug | DAP integration, summon AI at a breakpoint with live state |
| `08_QUICK_ACTIONS.md` | Quick Actions Sidebar | Context-sensitive programming task buttons, BDD wizard |
| `09_GRAPH_ENGINE_ROADMAP.md` | Graph Engine Roadmap | Priority plan for bringing `backend/graph` toward feature parity with the bundled graph reference |
| `11_DEEP_AGENTS_RUNTIME.md` | Deep Agents Runtime | Multi-provider runtime, skills, subagents, checkpoints, Ollama lane |
| `12_PYTHON_BRIDGE_MCP_SKILLS.md` | Python Bridge MCP + Skills | Bridge contract for runtimes, MCPs, skills, filesystem harnessing |

---

## Session Lifecycle

```
1. INIT
   └── User opens PairProtocol panel
   └── Extension indexes workspace (Tree-sitter + embeddings)
   └── Index stored in .waterfree/index.json

2. PLANNING
   └── User enters planning prompt (goal statement)
   └── AI reads index, generates structured plan (JSON)
   └── Plan rendered as editable task list in sidebar
   └── User reviews, edits, approves plan
   └── Both parties now share identical goal context

3. NAVIGATION
   └── Extension opens P0 (first task) file at correct line
   └── AI writes intent annotations above relevant code blocks
   └── Annotations are collapsible, non-blocking

4. NEGOTIATION LOOP (per task)
   └── Human reads annotation
   └── Human: Approve → AI executes, moves to next annotation
   └── Human: Alter → AI revises intent, re-annotates
   └── Human: Redirect → AI discards plan fragment, replans from new instruction
   └── Human adds TODO comment → AI queues it as instruction
   └── On file save → Side Effect Watcher runs, flags issues

5. COMPLETION
   └── Task marked complete in sidebar
   └── AI scans for ripple effects from completed task
   └── Next task begins, or AI asks clarifying question if ambiguous

6. SESSION PERSISTENCE
   └── Full session state serialized to .waterfree/session.json
   └── Resumable across VS Code restarts
```

---

## Key Design Decisions

### Pull, not push
The AI never volunteers output unless it is the AI's designated turn in the protocol. The human is never interrupted mid-thought. Ghost text is off. The AI speaks through structured annotations and questions only.

### The file is the communication channel
The developer does not need to switch to a chat panel to give instructions. `// TODO: [instruction]` comments written anywhere in the file are detected and queued. This keeps the developer in the editor at all times.

### Intent before action
The AI is architecturally forbidden from editing files before its intent annotation has been approved. The annotation step is not optional. This eliminates the most common failure mode: AI makes a change the developer didn't understand or want.

### The plan is mutable
Both the human and the AI can modify the task list at any time. New tasks can be inserted. Existing tasks can be reprioritised or deleted. The AI re-reads the plan before starting each task to ensure it has not drifted.

### Shared context is explicit
The codebase index is shown to the user as a readable tree + summary, not a black box. The user knows exactly what the AI knows. If the AI lacks context on a file, the user can explicitly add it to the session context.

---

## LLM Integration

- **Primary model:** Claude (claude-sonnet-4-5 or claude-opus-4-5 for planning)
- **API:** Anthropic Messages API with streaming
- **Context strategy:** Structured context injection per turn (index summary + relevant file chunks + session state + task description)
- **System prompt:** Enforces pair programming protocol — AI must annotate before acting, must ask rather than assume, must flag side effects explicitly
- **Temperature:** 0.2 for code generation, 0.5 for planning/questions
- **Planned expansion:** provider-neutral runtime with optional Deep Agents orchestration, Ollama local knowledge lane, and optional MCP-backed web search

---

## Technology Stack

| Component | Technology |
|---|---|
| Extension runtime | Node.js (VS Code Extension Host) — thin shell only |
| Backend intelligence | Python 3.10+ subprocess, communicates via stdio JSON-RPC |
| Parsing | tree-sitter 0.25+ with individual language packages (tree-sitter-python, tree-sitter-typescript, tree-sitter-javascript) |
| Code graph | Custom adjacency map built from Tree-sitter AST (Python) |
| LLM | Anthropic SDK (Python), tool use for guaranteed JSON output |
| Sidebar task list | VS Code TreeView API |
| Quick Actions sidebar | VS Code TreeView API (second view in same container) |
| Annotations | VS Code DecorationTypes + CodeLens API |
| Debug integration | VS Code Debug Adapter Protocol (DebugAdapterTrackerFactory) |
| File watching | VS Code FileSystemWatcher |
| Session state | JSON serialised to .waterfree/sessions/current.json |
| Build | esbuild (fast, VS Code standard) |
| TypeScript | Strict mode, ES2022, Node16 module resolution |

---

## Repository Structure (Actual)

```
PairProgram/
├── .waterfree/               # workspace session data (gitignored)
│   └── sessions/current.json    # serialised PlanDocument
├── backend/                     # Python — all intelligence
│   ├── requirements.txt
│   ├── server.py                # stdio JSON-RPC entry point (14 methods)
│   ├── indexer/
│   │   ├── parser.py            # tree-sitter AST + regex fallback
│   │   ├── code_graph.py        # dependency adjacency + ripple BFS
│   │   └── index_manager.py     # workspace indexer, caching, threading
│   ├── llm/
│   │   ├── claude_client.py     # Anthropic SDK, tool use, all call types
│   │   ├── prompt_templates.py  # system prompts per call type
│   │   └── context_builder.py   # per-turn context assembly
│   ├── session/
│   │   ├── models.py            # PlanDocument, Task, IntentAnnotation, enums
│   │   └── session_manager.py   # save/load/archive sessions
│   ├── negotiation/
│   │   ├── turn_manager.py      # 8-state AI state machine
│   │   └── negotiation_controller.py  # alter/redirect/skip/queue
│   └── debug/
│       └── live_debug.py        # DebugContext model + LLM formatting
├── src/                         # TypeScript — VS Code shell only
│   ├── extension.ts             # entry point + PairProtocolController
│   ├── bridge/
│   │   └── PythonBridge.ts      # spawns backend, async JSON-RPC client
│   ├── debug/
│   │   └── LiveDebugCapture.ts  # DAP tracker + capture()
│   ├── ui/
│   │   ├── PlanSidebar.ts       # TreeView: task list
│   │   ├── QuickActionsProvider.ts  # TreeView: context-sensitive buttons
│   │   ├── DecorationRenderer.ts    # CodeLens + gutter decorations
│   │   └── StatusBarManager.ts      # AI state + status bar
│   └── watchers/
│       ├── FileWatcher.ts       # debounced file change → updateFile
│       └── TodoWatcher.ts       # [wf] TODO detection on save
├── docs/                        # design documents
├── package.json                 # VS Code extension manifest
├── tsconfig.json
└── esbuild.config.js
```

---

## Known Gaps — Professional Readiness

### P0 — Blockers (must fix before "professional" label)

| Gap | Description |
|---|---|
| No code execution | `approveAnnotation` marks the annotation approved but never executes the code. `execute_task()` exists in `claude_client.py` but is never called. Need `handle_execute_task` in `server.py` and a TypeScript trigger post-approval. |
| Debug task integration bug | "Add as Task" in Live Pair Debug calls `createSession` (creates a new session) instead of appending the suggested fix to the existing session. |

### P1 — Core Quality

| Gap | Description |
|---|---|
| Side effect scanner empty | `TurnManager` transitions to SCANNING after execution, but no ripple scan logic exists in the backend. |
| No backend request timeout | `PythonBridge` hangs forever if Python crashes mid-request. Need a 30s timeout + auto-restart. |
| Inline decorations partial | `DecorationRenderer` registers CodeLens correctly but gutter highlight at target line is not rendering. |

### P2 — Polish

| Gap | Description |
|---|---|
| No token/cost tracking | Session has no record of API usage; developers are blind to spend. |
| No pre-edit file snapshots | No undo path after AI execution. |
| CodeLens line anchoring | Annotation line numbers shift when the developer edits the file. |

---

## Out of Scope (v1)

- Multi-developer collaboration (human + human + AI)
- Terminal command execution
- Automated test running (test suite generation is in-scope; running them is not)
- Git integration beyond file awareness
- Support for JetBrains or other IDEs
- Cloud-hosted session state
