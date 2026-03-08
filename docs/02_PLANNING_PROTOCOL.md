# Subsystem 02 — Planning Protocol & Session Manager
## WaterFree VS Code Extension

---

## Purpose

The planning phase establishes the shared mental model that makes everything else work. Before a single annotation is written, both the human and the AI must agree on:

- What the goal is
- What the codebase currently looks like relevant to that goal
- What the ordered set of tasks is
- What "done" means

The session manager then maintains this shared state for the entire session, ensuring neither party drifts from the agreed plan without it being a conscious decision.

---

## Components

### PlanningPanel.ts — The Opening Ritual

A full-screen Webview panel that appears when the user starts a new session. This is not a chat box. It is a structured form that forces both parties to be explicit.

**Panel sections:**

**1. Goal Statement**
```
What are we building / fixing / refactoring?
[ Free text area — no limit ]

Example: "Add rate limiting to the authentication endpoints. 
Max 5 login attempts per IP per 15 minutes. Use Redis for 
the counter. Return 429 with a Retry-After header."
```

**2. Scope Declaration**
```
Which parts of the codebase are in scope?
[ Multi-select from indexed module list ]
[ OR: "Infer from goal" checkbox ]

Out of scope (do not touch):
[ Free text — user can explicitly protect areas ]
```

**3. Constraints**
```
Style / approach requirements:
[ Free text — e.g. "no new dependencies", "functional style only",
  "must work on Node 18", "follow existing error handling pattern" ]
```

**4. AI Index Review**
Before generating the plan, the AI outputs a short "what I see" summary:
```
┌─ AI Codebase Read ──────────────────────────────────────┐
│                                                          │
│  I can see:                                              │
│  • AuthController at src/auth/AuthController.ts          │
│  • Existing middleware stack in src/middleware/           │
│  • Redis client already configured in src/config/redis.ts│
│  • No existing rate limiting code                        │
│  • Tests in __tests__/auth/ using Jest                   │
│                                                          │
│  I cannot see / am uncertain about:                      │
│  • Whether Redis is available in the test environment    │
│  • The deployment target (affects Redis connection config)│
│                                                          │
│  Questions before planning:                              │
│  • Should rate limiting apply to /auth/refresh as well?  │
│                                                          │
│  [ Answer questions ]  [ Skip, I'll add context below ]  │
└──────────────────────────────────────────────────────────┘
```

This "what I see / what I don't know" section is critical. It surfaces AI uncertainty *before* planning, not mid-execution.

---

### PlanDocument.ts — The Living Plan

The plan is a first-class data structure, not a chat message. It is mutable by both parties.

```typescript
interface PlanDocument {
  id: string;                      // UUID
  createdAt: number;
  lastModifiedAt: number;
  goalStatement: string;
  constraints: string[];
  scopedFiles: string[];
  outOfScope: string[];
  tasks: Task[];
  sessionNotes: SessionNote[];     // running log of decisions made
  status: 'planning' | 'active' | 'paused' | 'complete';
}

// ── Code Coordinates ──────────────────────────────────────────────────────────
// A precise pointer into source code. Line is optional — if omitted, the system
// uses the symbol name to locate the target at runtime (more resilient to edits).

type CoordAnchorType = 'create-at' | 'modify' | 'delete' | 'read-only-context';

interface CodeCoord {
  file: string;                    // relative workspace path
  class?: string;                  // class name (if applicable)
  method?: string;                 // method/function name
  line?: number;                   // line hint — symbol name takes priority if line drifts
  anchorType: CoordAnchorType;
}

// ── Task Priority ──────────────────────────────────────────────────────────────
// Priorities have defined behavioural meaning, not just ordering.
//   P0 — blocker: nothing else can proceed until this is done
//   P1 — critical path: must complete in the current session/milestone
//   P2 — should do this session: important but not blocking
//   P3 — backlog: deferred, visible but collapsed in sidebar by default
//   spike — research/decision task: produces a decision, not code

type TaskPriority = 'P0' | 'P1' | 'P2' | 'P3' | 'spike';

// ── Dependency ────────────────────────────────────────────────────────────────
//   blocks      — hard: cannot start until dependency completes
//   informs     — soft: output of that task changes how this one is done
//   shares-file — warns of conflict risk if both tasks are worked in parallel

interface TaskDependency {
  taskId: string;
  type: 'blocks' | 'informs' | 'shares-file';
}

// ── Owner ─────────────────────────────────────────────────────────────────────

interface TaskOwner {
  type: 'human' | 'agent' | 'unassigned';
  name: string;                    // e.g. "Steve", "Claude QA Agent"
  assignedAt?: number;
}

// ── Task ──────────────────────────────────────────────────────────────────────

interface Task {
  id: string;
  title: string;                   // short label for sidebar
  description: string;             // full description of what to do
  rationale: string;               // why this task, why in this order

  // Code location — where the work happens
  targetCoord: CodeCoord;          // primary location: file + optional class/method/line
  contextCoords?: CodeCoord[];     // read-only locations opened in split view during this task

  // Scheduling
  priority: TaskPriority;
  phase?: string;                  // milestone grouping label e.g. "Phase 1: DB Layer"
  dependsOn: TaskDependency[];     // replaces the old string[] — includes dependency type
  blockedReason?: string;          // human-readable explanation shown in sidebar tooltip

  // Ownership
  owner: TaskOwner;
  taskType: 'impl' | 'test' | 'spike' | 'review' | 'refactor';

  // Effort tracking — AI estimates, not shown as promises
  estimatedMinutes?: number;
  actualMinutes?: number;          // filled on complete, builds calibration data over sessions

  // Lifecycle
  status: 'pending' | 'annotating' | 'negotiating' | 'executing' | 'complete' | 'skipped';
  humanNotes?: string;
  aiNotes?: string;
  annotations: IntentAnnotation[]; // see Subsystem 03
  sideEffectWarnings: SideEffectWarning[];
  startedAt?: number;
  completedAt?: number;
}

interface SessionNote {
  timestamp: number;
  author: 'human' | 'ai';
  content: string;
  relatedTaskId?: string;
}
```

**Plan generation prompt template** (sent to Claude after planning form is submitted):

```
You are an expert software developer about to pair program with a human developer.
You have read the following codebase index:

[INDEX SUMMARY from IndexManager.getIndexSummary()]

The goal for this session is:
[GOAL STATEMENT]

Constraints:
[CONSTRAINTS]

In scope:
[SCOPED FILES]

Do NOT touch:
[OUT OF SCOPE]

Generate a structured implementation plan as a JSON array of Task objects.

Rules for the plan:
1. Tasks must be ordered — each task should be completable before the next begins
2. Each task MUST include a targetCoord with file + method/class (line is optional but preferred)
3. Include contextCoords for any files the developer needs to see but not edit while working
4. The first task must be P0 priority and the smallest possible starting point
5. Assign priority (P0/P1/P2/P3/spike) based on blocking relationships, not just sequence
6. Express dependencies with type: 'blocks', 'informs', or 'shares-file'
7. Include a rationale for each task's position in the sequence
8. Flag any task that will affect code outside the stated scope as a WARNING in aiNotes
9. If you are uncertain about any aspect, include a question in aiNotes
10. Maximum 12 tasks for v1. If more are needed, plan the first phase only.

Respond with ONLY valid JSON matching the Task[] schema. No preamble.
```

---

### TaskQueue.ts — Sequencing and State

Manages which task is active, what's next, and transitions between states.

```typescript
class TaskQueue {
  getCurrentTask(): Task
  getNextTask(): Task | null
  getPendingTasks(): Task[]
  getCompletedTasks(): Task[]
  
  // Move current task to next state in lifecycle
  advanceTaskState(): void
  
  // Human or AI inserts a new task (e.g. from a TODO comment)
  insertTask(task: Partial<Task>, afterTaskId?: string): Task
  
  // Human reorders via drag in sidebar
  reorderTask(taskId: string, newIndex: number): void
  
  // Human marks a task as skip (don't do this)
  skipTask(taskId: string, reason: string): void
  
  // AI requests to split a task (discovered more complexity)
  splitTask(taskId: string, subtasks: Partial<Task>[]): Task[]
  
  // Fired whenever queue state changes — UI subscribes to this
  onQueueChanged: EventEmitter<Task[]>
}
```

**Task lifecycle state machine:**
```
pending 
  → annotating      (AI has started writing intent annotations)
  → negotiating     (human is reviewing annotations)
  → executing       (human approved, AI is writing code)
  → complete        (code written, side effects checked)
  
  OR at any point:
  → skipped         (human explicitly skipped)
  
  From negotiating:
  → annotating      (human altered/redirected, AI re-annotates)
```

---

### SessionManager.ts — Persistent State Orchestrator

```typescript
class SessionManager {
  // Start fresh session
  async startSession(input: PlanningFormInput): Promise<PlanDocument>
  
  // Resume session from .waterfree/session.json
  async resumeSession(): Promise<PlanDocument | null>
  
  // Save current state (called frequently, debounced)
  async saveSession(): Promise<void>
  
  // Human edited plan directly in sidebar
  async applyHumanPlanEdit(edit: PlanEdit): Promise<void>
  
  // AI discovered something that requires plan change
  async requestPlanRevision(reason: string, proposedChanges: Partial<Task>[]): Promise<void>
  // ^ this surfaces a notification to the human, doesn't auto-apply
  
  // Called by NavigationManager when task starts
  async activateTask(taskId: string): Promise<void>
  
  // Called when task is complete
  async completeTask(taskId: string, summary: string): Promise<void>
  
  // Add a note to the session log
  addSessionNote(content: string, author: 'human' | 'ai', relatedTaskId?: string): void
  
  // Get everything the AI needs to start the next annotation
  buildTaskContext(taskId: string): TaskContext
}
```

---

### NavigationManager.ts — Taking the Human to the Code

When a task becomes active, the human is navigated to the relevant location automatically.

```typescript
class NavigationManager {
  async navigateToTask(task: Task): Promise<void>
  // 1. Opens task.targetCoord.file in the main editor group
  // 2. Resolves position: uses task.targetCoord.line as a hint, but
  //    falls back to symbol search (class::method) if line has drifted
  // 3. Reveals and centers on the target symbol
  // 4. Highlights the method/class block for 2 seconds
  // 5. Opens each task.contextCoords entry as read-only in split view
  // 6. Updates sidebar to show the active task

  async navigateToAnnotation(annotation: IntentAnnotation): Promise<void>
  // Resolves annotation.targetCoord using same symbol-first strategy

  async resolveCoord(coord: CodeCoord): Promise<vscode.Position>
  // Primary: search for coord.class / coord.method in the parsed index
  // Fallback: use coord.line directly
  // This makes navigation resilient to lines shifting due to edits above the target

  async openSplitContext(coord: CodeCoord): Promise<void>
  // Opens coord.file read-only in right split, positioned at coord symbol
}
```

**Navigation behaviour:**
- File is opened in the main editor group
- Symbol name is used to locate the target (resilient to line drift)
- A 2-second subtle highlight pulses on the target function/class block
- Context coords open read-only in split view so the developer sees dependencies without accidentally editing them
- The sidebar task list updates to show the active task
- A status bar item shows: `WaterFree: [Task Title] (2/7)`

---

## Planning Sidebar (TreeView)

The plan is rendered in the VS Code Explorer sidebar as a TreeView. This is always visible during a session.

**Visual structure:**
```
WATERFREE SESSION
├── Goal: Add rate limiting to auth endpoints
│
├── PHASE 1: Middleware Layer             [2/5 complete]
│   ├── ✅ 1. [P0] Add Redis counter utility         src/config/redis.ts::RedisClient.incr
│   ├── ▶  2. [P1] Create RateLimiter middleware     src/middleware/RateLimiter.ts  ← current
│   ├── ○  3. [P1] Inject middleware into router     src/routes/auth.ts::router
│   ├── ○  4. [P2] Add 429 response formatter        src/errors/RateLimitError.ts  [blocked by 3]
│   └── ○  5. [P1] Write unit tests                  tests/middleware/
│
└── PHASE 2: Integration                 [not started]
    └── ⚠️  6. [P2] Update integration tests         [warning - out of scope?]
```

**TreeItem states:**
- `○` — pending
- `◐` — annotating (AI writing intent)
- `👁` — negotiating (awaiting human review)
- `▶` — executing (approved, AI writing code)
- `✅` — complete
- `⊘` — skipped
- `⚠️` — warning (needs attention)

**Context menu on each task:**
- View full description
- Edit task description
- Insert task after this one
- Skip this task
- Move up / move down
- Add note

**Drag and drop:** Tasks can be reordered by drag. The AI is notified of the reorder and re-evaluates whether dependencies are still satisfied.

---

## Plan Edit Protocol

When the human edits a task (description, order, adds a task), the system does not silently accept it. It follows a mini-negotiation:

1. Human makes edit
2. System sends diff to AI: "The human has modified the plan. Here is what changed: [diff]. Does this change affect any other tasks? Are there dependency issues? Reply with: ACKNOWLEDGED or CONCERN: [explanation]"
3. If AI responds with CONCERN, a non-blocking notification appears: "AI flagged a concern about this plan change. [View]"
4. Human can dismiss the concern or view and respond to it
5. Session note is added recording the change and any concern

This keeps the AI's awareness of the plan current without blocking the human.

---

## Session Resume

On extension activation, check for `.waterfree/session.json`:

```typescript
const resumePrompt = await vscode.window.showInformationMessage(
  `WaterFree: Resume session "${session.goalStatement.slice(0, 50)}..."?`,
  'Resume',
  'Start New',
  'Discard'
);
```

On resume, navigate directly to the last active task.

---

## Configuration

```json
{
  "waterfree.session.autoSaveIntervalMs": {
    "type": "number",
    "default": 5000
  },
  "waterfree.session.maxTasksPerPlan": {
    "type": "number", 
    "default": 12
  },
  "waterfree.planning.model": {
    "type": "string",
    "default": "claude-opus-4-5",
    "description": "Model used for initial planning (can be more powerful than execution model)"
  },
  "waterfree.planning.executionModel": {
    "type": "string",
    "default": "claude-sonnet-4-5"
  }
}
```
