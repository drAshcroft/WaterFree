# Subsystem 07 — Todo System & Task Store
## WaterFree VS Code Extension

---

## Purpose

The todo system is the persistent, hierarchical task registry that outlives any single session. Where `sessions/current.json` holds the active plan for the current goal, `tasks.db` holds the durable backlog: deferred items, auto-generated tasks from source code signals, cross-session work, and multi-owner task coordination.

Every task — whether session-local or cross-session — shares the same `Task` shape (defined in Subsystem 02), including `CodeCoord` for precise source location, `priority`, `phase`, `owner`, and dependency graph.

---

## Storage

**Database:** `.waterfree/tasks.db`

This SQLite database lives in the workspace-local `.waterfree/` directory alongside `index.db`. It is workspace-local and gitignored by default.

**Relationship to session tasks:**
- Session tasks (`current.json`) are the active sprint — what is being worked on right now
- `tasks.db` is the backlog — everything else
- When a session ends, its incomplete tasks can be promoted to `tasks.db` rather than discarded
- When starting a new session, the planning panel can import tasks from `tasks.db` into the session

---

## TaskStore.ts

The single interface for reading and writing tasks across sessions.

```typescript
class TaskStore {
  // Load the full store from disk
  async load(): Promise<TaskStoreData>

  // Save current state (debounced, called on every mutation)
  async save(): Promise<void>

  // CRUD
  async addTask(task: Partial<Task>): Promise<Task>
  async updateTask(taskId: string, patch: Partial<Task>): Promise<Task>
  async deleteTask(taskId: string): Promise<void>

  // Querying
  getTasksByPriority(priority: TaskPriority): Task[]
  getTasksByPhase(phase: string): Task[]
  getTasksByOwner(ownerName: string): Task[]
  getBlockedTasks(): Task[]           // tasks whose dependsOn have type 'blocks' and are incomplete
  getReadyTasks(): Task[]             // tasks with no outstanding blockers
  getNextForOwner(ownerName: string): Task | null  // highest-priority ready task for this owner

  // Phase management
  addPhase(name: string): void
  getPhases(): string[]

  // Import / export between session and backlog
  async promoteToSession(taskId: string, sessionId: string): Promise<void>
  async demoteToBacklog(taskId: string): Promise<void>
  async importFromSession(session: PlanDocument): Promise<void>
}

interface TaskStoreData {
  version: number;
  tasks: Task[];
  phases: string[];
  updatedAt: string;
}
```

---

## Priority Definitions

Priority is not just an ordering label — it has enforced behavioural meaning in the UI and session flow.

| Priority | Meaning | Sidebar Behaviour | Session Behaviour |
|---|---|---|---|
| **P0** | Blocker — nothing else can proceed | Red badge, status bar alert | AI refuses to move to next task until P0 is resolved |
| **P1** | Critical path — current milestone | Shown first, always visible | Included in session by default |
| **P2** | Should do this session | Normal position in queue | Included in session if capacity allows |
| **P3** | Backlog — defer | Collapsed by default in sidebar | Not included in session auto-import |
| **spike** | Research task, produces a decision | Different icon (lightbulb), no targetCoord required | Creates a session note with the outcome when complete |

---

## CodeCoord in Tasks

Every non-spike task requires a `targetCoord`. This is the precise source location the task addresses.

```typescript
// Minimal valid task coord — file + method is enough
{ file: "src/db/client.ts", method: "connect", anchorType: "modify" }

// With class context
{ file: "src/db/client.ts", class: "DbClient", method: "connect", anchorType: "modify" }

// With line hint (optional — system uses symbol name if line drifts)
{ file: "src/db/client.ts", class: "DbClient", method: "connect", line: 42, anchorType: "modify" }

// Create-new task — file and method name to create, no current line
{ file: "src/errors/RateLimitError.ts", class: "RateLimitError", anchorType: "create-at" }
```

**Spike tasks** do not require a `targetCoord` — they produce a decision, not code.

---

## Auto-Task Generation

Three sources can automatically create tasks in the store without human intervention:

### 1. Source code TODO signals (via TodoWatcher)

```
// TODO: [wf] don't throw here, return a Result type instead
# TODO: [wf] handle the case where Redis is down
```

When the `TodoWatcher` detects a `[wf]`-tagged comment on save:
1. Extracts `file`, `line`, and `instruction` (already implemented in `TodoWatcher.ts`)
2. Resolves the symbol at that line via the parsed index to populate `targetCoord.method` and `targetCoord.class`
3. Creates a `Task` with `priority: 'P2'`, `taskType: 'impl'`, `owner: { type: 'unassigned' }`
4. Writes to `tasks.db`
5. Shows notification: "WaterFree: TODO queued as task. [View] [Assign to me] [Dismiss]"
6. Removes the TODO comment from the file (so it stays clean)

### 2. Side effect warnings (via SideEffectWatcher)

The "Add all warnings as tasks" button in the side effect panel (Subsystem 05) calls:

```typescript
sideEffectWatcher.onAddWarningsAsTasks(report: SideEffectReport): void
// Converts each RankedNode warning into a Task with:
//   targetCoord: { file: node.filePath, method: node.functionName, anchorType: 'modify' }
//   priority: node.impactScore > 0.7 ? 'P1' : 'P2'
//   taskType: 'review'
//   description: node.riskReason
```

### 3. Manual creation

Via command `waterfree.addTask` or the sidebar "+" button. Opens a form with:
- Task description (free text)
- File picker → method picker (populates `targetCoord` from the index)
- Priority selector
- Owner selector
- Phase selector

---

## "What Should I Do Next?" Command

Command: `waterfree.whatNext` (keybind: `Alt+W`)

Algorithm:
1. Get current user identity from config (`waterfree.ownerName`)
2. Filter `tasks.db` to tasks where `owner.name === me OR owner.type === 'unassigned'`
3. Remove tasks with outstanding `blocks`-type dependencies
4. Sort by: P0 first, then P1, then by `startedAt` (in-progress tasks surface first)
5. Open the top result: navigate to `targetCoord`, show a mini panel:

```
┌─ Next Task ──────────────────────────────────────────────────────┐
│ [P1] Add connection pooling to DbClient                          │
│ src/db/client.ts :: DbClient.connect                             │
│                                                                  │
│ Depends on: ✅ "Create postgres client" (complete)               │
│ Blocked by: nothing                                              │
│                                                                  │
│ [Start this task]   [Skip]   [Assign to someone else]            │
└──────────────────────────────────────────────────────────────────┘
```

---

## Phase Management

Phases are milestone groupings. They are labels only — they do not affect execution order. A task belongs to at most one phase.

Phases are stored in `tasks.db` metadata:
```json
{ "phases": ["Phase 1: DB Layer", "Phase 2: API Layer", "Phase 3: Tests"] }
```

**Sidebar rendering with phases:**
```
PHASE 1: DB Layer                    [2/4 complete]
  ✅ 1. [P0] Create postgres client   Steve       src/db/client.ts::DbClient
  ✅ 2. [P1] Schema migrations        Steve       src/db/migrations/
  ▶  3. [P1] Connection pooling       Claude      src/db/client.ts::DbClient.connect
  ○  4. [P1] BDD tests               Claude QA   tests/db/  [blocked by 3]

PHASE 2: API Layer                   [not started]
  ○  5. [P1] REST endpoints           unassigned  src/api/routes/
  ○  6. [P2] Request validation       unassigned  src/api/middleware/
```

Clicking a phase header collapses/expands all tasks in that phase.

---

## Dependency Visualisation

When hovering a task in the sidebar, the dependency chain highlights:
- Tasks that block this one (ancestors) — shown in amber
- Tasks this one blocks (descendants) — shown in blue
- Tasks sharing a file — shown with a shared-file indicator

This uses VS Code's `TreeItem.resourceUri` and decoration APIs — no separate canvas.

---

## Session Import from Backlog

When starting a new session, the planning panel shows a "From backlog" section:

```
┌─ Import from backlog? ─────────────────────────────────────────┐
│ 3 tasks ready (no blockers, assigned to you or unassigned):    │
│                                                                │
│ ☑ [P1] Connection pooling      Phase 1   src/db/client.ts     │
│ ☑ [P1] BDD tests               Phase 1   tests/db/            │
│ ☐ [P2] REST endpoints          Phase 2   src/api/routes/      │
│                                                                │
│ [Import selected]   [Skip]                                     │
└────────────────────────────────────────────────────────────────┘
```

Imported tasks are copied into the session's `PlanDocument.tasks` with their coords intact. The original entries in `tasks.db` are marked `status: 'executing'` to prevent double-import.

---

## AI Command Protocol

The AI is not a passive recipient of tasks — it is an active participant in maintaining the task store. Every AI response is wrapped in an envelope that allows the AI to emit task operations as a side-effect of its primary work.

### Response Envelope

All AI responses use this shape:
```typescript
interface AIResponse<T> {
  payload: T;              // the primary response (Task[], IntentAnnotation, CodeEdit, etc.)
  taskCommands?: TaskCommand[];  // optional task store operations
}
```

The extension calls `TaskCommandProcessor.process(response.taskCommands, session)` after handling the primary payload.

### When the AI Should Use Each Command

| Phase | Common commands | Trigger |
|---|---|---|
| **Planning** | `add-task` (P3 backlog), `add-note` | Notices out-of-scope work or open questions |
| **Annotating** | `split-task`, `add-task`, `block-task`, `add-note` | Task is bigger than planned; prerequisite missing |
| **Executing** | `add-task` (follow-up), `add-note`, `update-task` | Left a TODO; deviated from annotation; has actual time |
| **Answering** | `add-note`, `update-task` | Answer reveals a plan change is needed |

### Human Approval Gates

Not all commands are applied silently:

| Command | Applied | Approval required |
|---|---|---|
| `add-task` | Immediately, notification shown | No — human can delete from sidebar |
| `update-task` | Immediately | No — for status/note/priority changes |
| `block-task` | Immediately | No — always conservative to block |
| `add-note` | Immediately, silently | No |
| `split-task` | **Held pending** | **Yes — modal approval dialog** |

`split-task` changes the plan shape and may invalidate the human's mental model of the session. It is the only command that blocks until the human responds.

### Example: AI Discovers Scope During Annotation

During annotation of "Add connection pooling", the AI notices the existing `DbClient.connect()` has no error handling and the retry logic will be broken by the pool change.

Its response:
```json
{
  "payload": { /* IntentAnnotation */ },
  "taskCommands": [
    {
      "op": "add-task",
      "task": {
        "title": "Fix DbClient.connect error handling",
        "description": "connect() swallows errors silently — pool acquisition failures will be invisible",
        "targetCoord": { "file": "src/db/client.ts", "class": "DbClient", "method": "connect", "anchorType": "modify" },
        "priority": "P1",
        "taskType": "impl",
        "owner": { "type": "unassigned", "name": "" }
      }
    },
    {
      "op": "add-note",
      "content": "Noticed during pooling annotation: DbClient.connect silently swallows errors. Added as P1 follow-up task.",
      "relatedTaskId": "current-task-id"
    }
  ]
}
```

The developer sees: a new P1 task appears in the sidebar, and a session note is logged. They can assign it, defer it, or fold it into the current task — their choice.

### Example: AI Splits an Underestimated Task

During annotation, the AI realises "Add BDD tests" is actually three distinct test suites requiring different fixtures.

```json
{
  "payload": { /* IntentAnnotation with questionsBeforeProceeding */ },
  "taskCommands": [
    {
      "op": "split-task",
      "taskId": "task-4",
      "reason": "BDD tests span connection, pooling, and error paths — each needs separate fixtures and setup",
      "subtasks": [
        { "title": "BDD tests: connection lifecycle", "targetCoord": { "file": "tests/db/connection.test.ts", "anchorType": "create-at" }, "priority": "P1" },
        { "title": "BDD tests: pool behaviour", "targetCoord": { "file": "tests/db/pool.test.ts", "anchorType": "create-at" }, "priority": "P1" },
        { "title": "BDD tests: error paths", "targetCoord": { "file": "tests/db/errors.test.ts", "anchorType": "create-at" }, "priority": "P2" }
      ]
    }
  ]
}
```

The developer sees a dialog: "AI wants to split task into 3 subtasks: [reason]. Approve Split / Reject." If approved, the original task is replaced. If rejected, the original stands and the AI proceeds with it as-is.

---

## Velocity Tracking

After each session completes, the store records:

```typescript
interface SessionVelocity {
  sessionId: string;
  date: string;
  tasksPlanned: number;
  tasksComplete: number;
  tasksDeferred: number;
  tasksSkipped: number;
  averageActualMinutes: number;   // across completed tasks with actualMinutes set
  p0AllComplete: boolean;
}
```

Stored as `velocityLog: SessionVelocity[]` in `tasks.db`. Shown at session end:

```
Session complete: 5/7 tasks done, 2 deferred to backlog
Average task time: 22 min  |  P0 tasks: all complete
```

Over multiple sessions, this calibrates the AI's `estimatedMinutes` against real data.

---

## Configuration

```json
{
  "waterfree.tasks.ownerName": {
    "type": "string",
    "default": "",
    "description": "Your name as it appears in task ownership (used by 'What Next?')"
  },
  "waterfree.tasks.autoImportThreshold": {
    "type": "string",
    "enum": ["P0", "P1", "P2", "none"],
    "default": "P1",
    "description": "Auto-include tasks at this priority or higher when starting a new session"
  },
  "waterfree.tasks.showP3InSidebar": {
    "type": "boolean",
    "default": false,
    "description": "Show P3 backlog tasks in sidebar (collapsed). False keeps sidebar focused on active work."
  }
}
```
