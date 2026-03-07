# WaterFree

> You architect. AI executes. Structured pair programming where you lead — free from the bottlenecks of waterfall.

WaterFree is a VS Code extension that structures the human-AI collaboration around a clean division of roles: **you own the plan, the AI owns the implementation**. You think like a waterfall architect — defining goals, approving intent, redirecting when needed. The AI works like a tireless engineer — annotating its approach before touching a line of code, then executing only what you've approved.

The name is a play on *waterfall*: you get the clarity of top-down design, minus the bureaucracy that makes waterfall slow.

---

## How It Works

```
You describe a goal
        ↓
  AI generates a task plan
        ↓
  You review & adjust the plan
        ↓
  For each task:
    AI writes an intent annotation (no code yet)
    You Approve / Alter / Redirect
    AI executes exactly what was approved
    AI scans for side effects
        ↓
  Repeat → goal complete
```

Every code change is preceded by a human-readable annotation — a plain-English description of *what* will change, *where*, and *why*. No surprises. No silent rewrites.

---

## Features

### Architect Mode (You)
- Describe a goal in plain language
- Review the AI's generated task breakdown
- Approve, alter, or redirect each annotation before a single line changes
- Take over any task yourself — the AI reads your changes and picks up seamlessly
- Inline `// TODO: [wf] <instruction>` comments are automatically queued as tasks

### Engineer Mode (AI)
- Indexes your codebase (AST + call graph) for accurate, context-aware suggestions
- Annotates intent before execution — what files, what lines, what effects
- Executes only approved annotations, nothing more
- Scans side effects after each edit using the dependency graph
- Live debug integration — push a breakpoint snapshot to the agent for diagnosis

### Knowledge Store
- Snippetize your workspace or any git repo into a searchable pattern library
- AI draws on your team's existing patterns before reaching for generic solutions
- Extract individual functions as reusable procedures

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

Traditional waterfall puts the human in charge of requirements and the team in charge of implementation — but the hand-off is lossy and slow.

WaterFree keeps the human in the architect seat while eliminating the friction. You define intent at a high level. The AI translates intent into precise, reviewable annotations. You approve. The AI executes. Every edit is a conversation, not a surprise.

**The AI never writes code you haven't read first.**
