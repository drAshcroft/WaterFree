# WaterFree


> Deliberate code, delivered faster.

Coding is supposed to be fun. AI coding can be fun.

WaterFree is a VS Code extension built around a simple conviction: **The fastest path to working software is a tight human-AI feedback loop, not an army of agents burning tokens on retries.**

It takes the best idea from waterfall — structured, top-down intent — and merges in agile's instant, continuous collaboration. You define the goal. The AI proposes the plan. You give feedback immediately, at every step, before a single line of code is written. When you finally say *go*, the implementation is already aligned with your intent.

The result: fewer iterations, fewer tokens, less rework. Everyone — AI, human, market — is on the same page from the start.

This an attempt to remove prompt engineering. Instead, the system handles context automatically (codebase index + knowledge store), and collaboration happens through **microprompts** — small, focused, hierarchical intent statements, one per task. Easy to write. Easy to correct. Impossible for AI to misread.


---


## The Core Idea


Traditional AI coding tools hand you a large diff and ask you to react. You review the output, spot what's wrong, re-prompt, and wait again. Every misalignment costs a full round-trip. It is incredibly frustrating to have a simple change take multiple rounds.


WaterFree inverts this. **The collaboration happens before execution**, not after.


```
You describe a goal
        ↓
  AI generates a task plan         ← you give feedback here
        ↓
  For each task:
    AI writes a microprompt (intent annotation)
    You Approve / Alter / Redirect
    AI executes exactly what was approved
    You are in the loop.  You can fix code, alter paths, learn and generate todos
    AI scans for side effects
    AI checks for edges and works with you
        ↓
  Goal complete
```


Every code change is preceded by a plain-English annotation — *what* will change, *where*, and *why*. You read it, refine it, scope it and approve it. The AI never writes code you haven't seen first. By the time execution runs, it's already right.


### What an intent annotation looks like


```
Task: Add rate limiting to the login endpoint


  Intent Annotation
  ─────────────────────────────────────────────────────
  File:    src/api/auth.ts
  Lines:   42–67  (handleLogin function)


  Change:  Import RateLimiter from src/utils/rate-limit.ts.
           Wrap the function body with a per-IP check (max 5 req/min).
           On limit exceeded, return 429 { error: "Too many requests" }.


  Why:     Prevent brute-force attacks on the login endpoint.


  Risk:    Low — RateLimiter is already used in src/api/register.ts:31,
           The same config pattern applies here.
  ─────────────────────────────────────────────────────
  [ Edit Microprompt Ctrl+Alt+E ] [Pseudo code it Ctrl+Alt+P] [ Code it Ctrl+Alt+A ] [ Rebuild plan Ctrl+Alt+R ]
```


You can approve in one keystroke, or push back with plain text. The AI revises and re-annotates before touching anything.


---


## Why This Works


Most AI coding friction comes from misalignment — the AI didn't know your conventions, misread the scope, or made an assumption you'd have corrected in five seconds if asked. You end up in a loop: review, re-prompt, re-review.


WaterFree eliminates that loop by giving the AI everything it needs upfront:


- **A live codebase index** — AST + call graph so the AI knows your actual structure, not a guess
- **A knowledge store** — your team's extracted patterns, so the AI reaches for *your* solutions first
- **Microprompts, not megaprompts** — instead of one giant prompt that tries to describe everything and still produces confusing code, each task gets its own focused microprompt. The AI writes it; you edit or approve it. Small surface area means fast feedback and no ambiguity.

Set the system up correctly, and the AI gets it right the first time. That's not a claim about AI capability — it's a claim about information. The codebase index and knowledge store replace what you used to pack into your prompt. The microprompt handles the rest — one task, one decision, one approval.


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


## Built for Experienced Engineers. Develops Junior Engineers.


Most AI coding tools frame experience as optional — just let the AI figure it out. WaterFree works the opposite way: **Experience and Context is the input that makes the system work.**


The knowledge store encodes your team's conventions, patterns, and hard-won decisions. The annotation review is where architectural judgment gets applied. The AI plays the junior engineer — it reads the docs, handles the boilerplate, follows the patterns you've set, and most importantly, it asks followup questions. You play the senior — you define scope, review intent, and get your coding hat when something's off.


**For junior developers, this is a forcing function.** Instead of jumping straight into code, they have to engage at the level of structure, architecture, and content — the high-level concerns that define proper software development. The annotation review asks them to think before approving: does this match the requirement? Is the scope right? What's the risk? That's the habit good engineers build over years. WaterFree builds it from the first session.


The AI and human are tightly coupled at every step — which is exactly what breaks the frustration loop. There's no moment where the AI disappears for 30 seconds and comes back with a mountain of unfathomable code and a bill. The collaboration is continuous, and disagreements are resolved before they become code.


---


## Getting Started


### Requirements
- VS Code 1.85+
- Python 3.10+ (for the backend)
- An Anthropic API key ([get one here](https://console.anthropic.com/))


### Setup


1. Install the extension
2. Run `WaterFree: Setup` and enter your Anthropic API key when prompted
3. Run `.\install.ps1` to install the runtime and register MCP servers (Claude/Codex). Use `-SkipVSCode` if you only want MCP servers.
4. To build an MSI installer (no Claude/Codex CLI required), run `.\installer\build-installer.ps1`. See `installer\README.md`.
4. Open a workspace and run **WaterFree: Start Session** from the command palette (`Ctrl+Shift+P`)


### First Session


 


## Commands


| Command | Description |
|---|---|
| `WaterFree: Setup` | Store the Anthropic API key in VS Code secure secret storage |
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
| `waterfree.anthropicApiKey` | `""` | Deprecated plaintext migration setting. Use `WaterFree: Setup` instead. |
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
- `WaterFree: Setup` stores the Anthropic API key in VS Code secure secret storage instead of plain settings
- the current developer deployment scripts store MCP-side provider secrets in a user-scoped Windows DPAPI store at `~/.waterfree/secrets.json`
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
- [docs/16_CUSTOMER_INSTALLER_PLAN.md](docs/16_CUSTOMER_INSTALLER_PLAN.md) — production installer plan for Windows packaging, Claude/Codex MCP registration, verification, repair, and uninstall


---


## MCP Tools


WaterFree exposes its internal tooling as MCP servers, making them available to Claude Code and other AI agents in your workspace:


| Server | Purpose |
|---|---|
| `waterfree-index` | Codebase graph: search symbols, trace callers/callees, detect change impact |
| `waterfree-knowledge` | Knowledge store: search extracted patterns and snippets |
| `waterfree-todos` | Task backlog: list, add, update, and complete tasks |
| `waterfree-debug` | Live debug: inspect breakpoint snapshots progressively |
| `waterfree-qa-summary` | Local QA summarizer: analyze a file or URL with Ollama and answer a question in detail |


---


## Philosophy


Waterfall's insight was sound: top-down structure, clear intent, defined roles. Its flaw was the hand-off — requirements thrown over a wall, feedback arriving months late.


WaterFree keeps the structure and collapses the feedback loop to zero. Every decision point — the plan, each annotation, each execution — is a live conversation between you and the AI. You stay in the architect seat. The AI stays in the engineer seat. And because the collaboration happens *before* the code, not after it, the output is right the first time.


This isn't about using fewer agents or tokens as an end goal. It's a consequence of working well: when the system has the right context and you validate intent before execution, you don't need to burn cycles on retries.


**Deliberate code, delivered faster.**



