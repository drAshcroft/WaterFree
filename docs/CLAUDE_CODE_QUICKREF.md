# WaterFree — Claude Code Quick Reference
## Pass this to Claude Code alongside the full subsystem docs

---

## What This Project Is

A VS Code extension implementing structured AI pair programming. The key differentiator: **the AI declares intent before acting, and the human approves or redirects before any code is written.**

Not a chatbot. Not an autocomplete. A negotiated protocol.

---

## Start Here

1. Read `00_OVERVIEW.md` for architecture and session lifecycle
2. The tech stack is: TypeScript, VS Code Extension API, Tree-sitter, Anthropic SDK, esbuild
3. Repo structure is defined in `00_OVERVIEW.md` under "Repository Structure"
4. Extension entry point is `src/extension.ts` (see `06_EXTENSION_SCAFFOLD.md` for full implementation)

---

## Architecture Note

The backend is **Python** (not TypeScript). TypeScript is a thin VS Code shell that spawns a Python process and communicates via newline-delimited JSON-RPC on stdin/stdout.

- `backend/server.py` — 14-method JSON-RPC server
- `src/bridge/PythonBridge.ts` — async JSON-RPC client

## Preferred Discovery Tools

When reasoning about the codebase, prefer the internal graph/indexer surface over grep-style discovery or broad file scans.

- Use `get_architecture` for entry points, layers, hotspots, clusters, and ADR context
- Use `index_repository` if the graph needs an explicit refresh before querying
- Use `get_graph_schema` to inspect supported labels, edge types, and graph shape
- Use `search_graph` for symbol lookup and graph-scoped discovery
- Use `search_code` for indexed text search
- Use `find_qualified_name` to resolve short symbol names before fetching snippets
- Use `get_code_snippet` for exact symbol source
- Use `trace_call_path` and `detect_changes` for caller, callee, and blast-radius analysis
- Use `query_graph` for targeted read-only graph queries
- Use `list_projects` and `index_status` to confirm the index is present and ready

Fallback policy:
- Only use grep, recursive file search, or broad raw-file reads when the index is missing, stale, or cannot answer the question.
- If you fall back, keep the scope narrow and say why.

## Document Map

| Doc | What it covers | Key types defined |
|---|---|---|
| `00_OVERVIEW.md` | Vision, actual architecture, session lifecycle, known gaps | - |
| `01_CODEBASE_INDEXING.md` | Tree-sitter parsing, code graph | `ParsedFile`, `CodeGraph`, `IndexManager` |
| `02_PLANNING_PROTOCOL.md` | Planning form, plan document, task queue | `PlanDocument`, `Task`, `SessionManager` |
| `03_INTENT_ANNOTATION.md` | Annotation rendering, approve/alter/redirect | `IntentAnnotation`, `DecorationRenderer` |
| `04_NEGOTIATION_PROTOCOL.md` | Turn management, state machine, execution | `TurnManager`, `NegotiationController` |
| `05_SIDE_EFFECT_WATCHER.md` | Ripple detection, style checking, error interpretation | `RippleReport`, `SideEffectWatcher` |
| `06_EXTENSION_SCAFFOLD.md` | package.json, extension.ts, build system | `ClaudeClient`, `ContextBuilder` |
| `07_LIVE_PAIR_DEBUG.md` | DAP integration, summon AI at breakpoint | `DebugContext`, `DebugAnalysis`, `LiveDebugCapture` |
| `08_QUICK_ACTIONS.md` | Context-sensitive sidebar buttons, BDD wizard | `QuickActionsProvider`, `QuickActionItem` |

---

## Core Data Types (summary)

```typescript
// The plan document — single source of truth for a session
interface PlanDocument {
  id: string;
  goalStatement: string;
  tasks: Task[];
  status: 'planning' | 'active' | 'paused' | 'complete';
}

// A single unit of work
interface Task {
  id: string;
  title: string;
  description: string;
  targetFile: string;
  targetLine?: number;
  status: 'pending' | 'annotating' | 'negotiating' | 'executing' | 'complete' | 'skipped';
  annotations: IntentAnnotation[];
}

// What the AI declares before acting
interface IntentAnnotation {
  id: string;
  taskId: string;
  targetFile: string;
  targetLine: number;
  summary: string;           // collapsed view — 1 sentence
  detail: string;            // expanded view — full explanation
  willCreate: string[];
  willModify: string[];
  sideEffectWarnings: string[];
  assumptionsMade: string[];
  questionsBeforeProceeding: string[];
  status: 'pending' | 'approved' | 'altered' | 'redirected';
}

// AI state machine states
type AIState = 'idle' | 'planning' | 'annotating' | 'awaiting_review' | 'executing' | 'scanning' | 'answering' | 'awaiting_redirect';
```

---

## Critical Implementation Rules

1. **AI never edits files unless AIState === 'executing'**
2. **Annotations are not written to disk** — they live in memory/session.json only
3. **All edits are applied as atomic WorkspaceEdit transactions**
4. **Index runs in a Worker thread** — never block the extension host
5. **Session state is serialised to `.waterfree/sessions/current.json`** after every state change
6. **The plan is mutable** — both human and AI can change it, but AI proposes, human confirms
7. **TODO comments with `[wf]` tag are treated as live directives** — not regular comments

---

## Build & Run

```bash
npm install
npm run watch    # incremental build
# F5 to launch extension development host in VS Code
npm test         # run tests
```

---

## Key VS Code APIs Used

| API | Used for |
|---|---|
| `vscode.window.createTreeView` | Plan sidebar task list |
| `vscode.window.createWebviewPanel` | Planning form, annotation detail panels |
| `vscode.languages.registerCodeLensProvider` | Collapsed annotation lines in editor |
| `vscode.window.createTextEditorDecorationType` | Gutter icons, line highlights |
| `vscode.workspace.applyEdit(WorkspaceEdit)` | Atomic code edits |
| `vscode.workspace.createFileSystemWatcher` | File change detection |
| `vscode.languages.onDidChangeDiagnostics` | Error interpretation trigger |
| `vscode.window.showTextDocument + revealRange` | Navigation to task locations |

---

## LLM Call Types

| Call type | Model | Temp | Max tokens |
|---|---|---|---|
| Plan generation | claude-opus-4-6 | 0.3 | 4096 |
| Intent annotation | claude-sonnet-4-6 | 0.2 | 1024 |
| Alter annotation | claude-sonnet-4-6 | 0.2 | 1024 |
| Code execution | claude-sonnet-4-6 | 0.1 | 8192 |
| Question answering | claude-sonnet-4-6 | 0.4 | 512 |
| Live debug analysis | claude-sonnet-4-6 | 0.2 | 1024 |
| File explanation | claude-sonnet-4-6 | 0.4 | 1024 |
| Style check | claude-sonnet-4-6 | 0.1 | 512 |
| Error interpretation | claude-sonnet-4-6 | 0.3 | 256 |

---

## Out of Scope for v1

- Terminal command execution
- Automated test running  
- Git integration
- Multi-developer (human+human+AI) sessions
- JetBrains support
- Cloud session sync

---

## Remaining Build Order (from current state)

Already built:
- ✅ Python backend: `server.py` (15 methods), `models.py`, `claude_client.py`, `session_manager.py`
- ✅ Python backend: `parser.py`, `code_graph.py`, `index_manager.py`
- ✅ Python backend: `turn_manager.py`, `negotiation_controller.py`
- ✅ Python backend: `live_debug.py`
- ✅ TypeScript shell: `extension.ts`, `PythonBridge.ts`, `PlanSidebar.ts`
- ✅ TypeScript shell: `DecorationRenderer.ts`, `StatusBarManager.ts`, `QuickActionsProvider.ts`
- ✅ TypeScript shell: `FileWatcher.ts`, `TodoWatcher.ts`, `LiveDebugCapture.ts`
- ✅ Code execution: `executeTask` backend method + `_applyEdits` WorkspaceEdit in TypeScript
- ✅ Debug task bug fixed: "Add as Task" appends to existing session, not a new one

Still to build (in priority order):
1. **[P1]** `QuickActionsProvider.ts` — context-sensitive sidebar with BDD wizard ✅ done
4. **[P1]** Backend timeout + restart in `PythonBridge.ts`
5. **[P1]** `RippleDetector` post-execution scan in `server.py`
6. **[P1]** Fix `DecorationRenderer` — gutter highlight at annotation target line
7. **[P2]** Token tracking in session notes
8. **[P2]** Pre-edit file snapshots + "Revert last AI edit" command
