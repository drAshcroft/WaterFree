# WaterFree

> Waterfall clarity. Instant feedback. Do it right the first time.

WaterFree is a VS Code extension built around a simple conviction: **the fastest path to working software is a tight human-AI feedback loop, not an army of agents burning tokens on retries.**

It takes the best idea from waterfall — structured, top-down intent — and replaces the slow hand-offs with instant, continuous collaboration. You define the goal. The AI proposes the plan. You give feedback immediately, at every step, before a single line of code is written. When you finally say *go*, the implementation is already aligned with your intent.

The result: fewer iterations, fewer tokens, less rework. Not because the AI is smarter — because the system is set up correctly on the first try.

---

## The Core Idea

Traditional AI coding tools hand you a diff and ask you to react. You review output, spot what's wrong, re-prompt, wait again. Every misalignment costs a full round-trip.

WaterFree inverts this. **The collaboration happens before execution**, not after.

```
You describe a goal
        ↓
  AI generates a task plan         ← you give feedback here
        ↓
  For each task:
    AI writes an intent annotation  ← and here, before any code
    You Approve / Alter / Redirect
    AI executes exactly what was approved
    AI scans for side effects
        ↓
  Repeat → goal complete
```

Every code change is preceded by a plain-English annotation — *what* will change, *where*, and *why*. You read it, refine it, and approve it. The AI never writes code you haven't seen first. By the time execution runs, it's already right.

---

## Why This Works

Most AI coding friction comes from misalignment — the AI didn't know your conventions, misread the scope, or made an assumption you'd have corrected in five seconds if asked. You end up in a loop: review, re-prompt, re-review.

WaterFree eliminates that loop by giving the AI everything it needs upfront:

- **A live codebase index** — AST + call graph so the AI knows your actual structure, not a guess
- **A knowledge store** — your team's extracted patterns, so the AI reaches for *your* solutions first
- **Intent annotations** — a checkpoint where you correct misalignment before it becomes code

Set the system up correctly, and the AI gets it right the first time. That's not a claim about AI capability — it's a claim about information. Give the AI the right context and a feedback loop before execution, and retries become rare.

---

## Features

### You Lead
- Describe a goal in plain language; the AI generates a structured task breakdown
- Review and adjust the plan before any work starts
- Approve, alter, or redirect each intent annotation before a line changes
- Take over any task yourself — the AI reads your changes and continues seamlessly
- Drop `// TODO: [wf]` comments anywhere; they're auto-queued when you save

### AI Executes with Full Context
- Indexes your codebase (AST + call graph) for accurate, dependency-aware suggestions
- Searches your knowledge store for existing patterns before writing anything new
- Writes an intent annotation per task — plain English, no code yet
- Executes only what you approved; scans for side effects after each edit
- Live debug integration — push a breakpoint snapshot for AI diagnosis

### Knowledge Store
- Snippetize your workspace or any git repo into a searchable pattern library
- AI draws on your team's existing conventions before reaching for generic solutions
- Extract individual functions as reusable, searchable procedures

---

## Getting Started

### Requirements
- VS Code 1.85+
- Python 3.10+ (for the backend)
- An Anthropic API key ([get one here](https://console.anthropic.com/))

### Setup

1. Install the extension
2. Set your API key:
   - Open **Settings** → search `waterfree.anthropicApiKey`, or
   - Set the `ANTHROPIC_API_KEY` environment variable
3. Open a workspace and run **WaterFree: Start Session** from the command palette (`Ctrl+Shift+P`)

### First Session

```
Ctrl+Shift+P → WaterFree: Start Session
→ Describe your goal: "Add JWT authentication to the API"
→ Review the generated task plan
→ Click "Annotate" on the first task
→ Review the intent annotation
→ Approve (Ctrl+Alt+A) → AI writes the code
→ Repeat for each task
```

---

## Commands

| Command | Description |
|---|---|
| `WaterFree: Start Session` | Describe a goal and generate a task plan |
| `WaterFree: Check/Index Workspace` | Index or refresh the codebase graph |
| `WaterFree Snippetize: Snippetize Workspace` | Extract patterns from the workspace |
| `WaterFree Snippetize: Snippetize Repo` | Extract patterns from a git repo or local path |
| `WaterFree Snippetize: Snippetize Procedure` | Extract a single function as a reusable snippet |
| `WaterFree: Push Debug Snapshot to Agent` | Capture a breakpoint state for AI diagnosis |

### Annotation Review Keybindings

| Key | Action |
|---|---|
| `Ctrl+Alt+A` | Approve annotation → execute |
| `Ctrl+Alt+E` | Alter annotation (give feedback, AI revises) |
| `Ctrl+Alt+R` | Redirect task (new direction, AI re-annotates) |

---

## Configuration

| Setting | Default | Description |
|---|---|---|
| `waterfree.anthropicApiKey` | `""` | API key (falls back to `ANTHROPIC_API_KEY` env var) |
| `waterfree.planningModel` | `claude-opus-4-6` | Model used for planning |
| `waterfree.executionModel` | `claude-sonnet-4-6` | Model used for annotation and execution |
| `waterfree.pythonPath` | `python` | Path to Python 3.10+ executable |
| `waterfree.rippleDepth` | `3` | Dependency hops to scan for side effects after each edit |
| `waterfree.graphBinaryPath` | `codebase-memory-mcp` | Path to the graph binary |

---

## Inline TODO Integration

Tag any comment with `[wf]` to queue it as a task the moment you save the file:

```typescript
// TODO: [wf] Add rate limiting to this endpoint
async function handleLogin(req, res) { ... }
```

```python
# TODO: [wf] Extract this into a shared utility and add tests
def parse_token(token):
    ...
```

WaterFree detects these on save and adds them to the active session's backlog automatically.

---

## Data & Privacy

- All session data is stored locally in `.waterfree/` inside your workspace (add to `.gitignore`)
- The codebase index, call graph, and session history never leave your machine except as context sent to the Anthropic API
- No telemetry is collected by WaterFree itself

```
.waterfree/
├── index.json          # Parsed file index
├── graph.json          # Call graph (adjacency list)
├── index.meta.json     # Hashes and timestamps for incremental re-indexing
├── sessions/           # Session history
├── debug/              # Debug snapshots
└── logs/               # Extension and backend logs
```

---

## Design Docs

- [docs/11_DEEP_AGENTS_RUNTIME.md](docs/11_DEEP_AGENTS_RUNTIME.md) — proposed multi-provider runtime with Deep Agents, first-class skills, subagents, checkpointing, and an Ollama lane for local knowledge work
- [docs/12_PYTHON_BRIDGE_MCP_SKILLS.md](docs/12_PYTHON_BRIDGE_MCP_SKILLS.md) — proposed bridge contract for MCP discovery, skill loading, subagent delegation, filesystem harnessing, and optional web-search/retrieval MCPs

---

## MCP Tools

WaterFree exposes its internal tooling as MCP servers, making them available to Claude Code and other AI agents in your workspace:

| Server | Purpose |
|---|---|
| `waterfree-index` | Codebase graph: search symbols, trace callers/callees, detect change impact |
| `waterfree-knowledge` | Knowledge store: search extracted patterns and snippets |
| `waterfree-todos` | Task backlog: list, add, update, and complete tasks |
| `waterfree-debug` | Live debug: inspect breakpoint snapshots progressively |

---

## Philosophy

Waterfall's insight was sound: top-down structure, clear intent, defined roles. Its flaw was the hand-off — requirements thrown over a wall, feedback arriving months late.

WaterFree keeps the structure and collapses the feedback loop to zero. Every decision point — the plan, each annotation, each execution — is a live conversation between you and the AI. You stay in the architect seat. The AI stays in the engineer seat. And because the collaboration happens *before* the code, not after it, the output is right the first time.

This isn't about using fewer agents or tokens as an end goal. It's a consequence of working well: when the system has the right context and you validate intent before execution, you don't need to burn cycles on retries.

**The fastest way to ship is to get it right before you start.**
