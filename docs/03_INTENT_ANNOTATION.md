# Subsystem 03 — Intent Annotation Layer
## WaterFree VS Code Extension

---

## Purpose

This is the most novel subsystem. It is the layer that makes PairProtocol feel fundamentally different from every other AI coding tool.

**Core rule:** The AI is architecturally prohibited from editing any file until its intent has been expressed as an annotation and the human has approved it.

Annotations are not comments in the traditional sense. They are structured, collapsible, interactive declarations of intent that live *alongside* the code — in the gutter and as decorations — without being written into the file itself until approved.

---

## What an Annotation Looks Like

In the editor, annotations appear as:

```
                    ┌─────────────────────────────────────────────┐
  Line 142  ───────▶│ 🤖 AI Intent  ▼                             │
                    │ Add a Redis-backed counter check before the  │
                    │ function body. If counter > 5, throw         │
                    │ RateLimitError. Counter key = IP + endpoint. │
                    │                                              │
                    │ Will touch: validateCard() caller chain      │
                    │ Creates: RateLimitError (new)                │
                    │                                              │
                    │  ✓ Approve    ✎ Alter    ✕ Redirect         │
                    └─────────────────────────────────────────────┘
  Line 142:  async function processPayment(req: Request) {
  Line 143:    const card = req.body.card;
```

The annotation floats *above* the target line using VS Code's inline decoration API. It is collapsible. It does not modify the file.

---

## Components

### IntentAnnotator.ts

Generates the annotation content by asking the AI what it plans to do for a given task at a given location.

**Annotation generation prompt:**
```
You are about to make a code change. Before writing any code,
you must declare your intent.

TASK: [task.description]
TARGET COORD: [task.targetCoord — file, class, method, line]
CONTEXT COORDS: [task.contextCoords — read-only reference locations]

CURRENT CODE AT TARGET:
[code snippet resolved from targetCoord, max 30 lines]

FULL CONTEXT:
[TaskContext from IndexManager — dependencies, callers, similar patterns]

Respond with a JSON IntentAnnotation object. Include:
- targetCoord: the exact CodeCoord where this change anchors (inherit from task,
  but narrow to the specific method line if you can)
- contextCoords: any additional locations the developer should see while reviewing
Be specific and brief. This is shown as a margin note.
They will approve, alter, or redirect based on what you write here.
Do NOT write any code yet.
```

**IntentAnnotation schema:**
```typescript
interface IntentAnnotation {
  id: string;
  taskId: string;

  // Where the annotation anchors — uses CodeCoord from Subsystem 02.
  // The renderer resolves targetCoord.method/class via the index to find the
  // exact line, making annotations resilient to edits above the target.
  targetCoord: CodeCoord;          // primary anchor: where the change happens
  contextCoords?: CodeCoord[];     // additional locations referenced in the intent

  summary: string;                 // 1-2 sentence plain English. This is the collapsed view.
  detail: string;                  // full explanation. Shown when expanded.
  approach: string;                // the specific technical approach

  willCreate: string[];            // new functions/classes/files to be created
  willModify: string[];            // existing things that will change
  willDelete: string[];            // things to be removed

  sideEffectWarnings: string[];    // things the AI noticed that might be affected
  assumptionsMade: string[];       // things the AI assumed (surfaces uncertainty)
  questionsBeforeProceeding: string[]; // if AI is unsure, it asks here

  status: 'pending' | 'approved' | 'altered' | 'redirected';
  humanResponse?: string;          // what the human said if altered/redirected

  createdAt: number;
  reviewedAt?: number;
}
```

**Multi-annotation tasks:**
Complex tasks may require multiple annotations at different locations. The AI generates them all upfront, and the human reviews them in sequence. The human can jump between them using the sidebar or keyboard shortcut.

---

### DecorationRenderer.ts

Renders the annotations visually using VS Code's Decoration API. This is the most technically complex piece of the UI.

**Coord resolution before rendering:**

Before placing a CodeLens or decoration, the renderer resolves the annotation's `targetCoord` to a concrete line number:
1. Query the parsed index for `coord.class::coord.method` — returns the current `startLine`
2. If the symbol is not found (e.g. file not yet indexed), fall back to `coord.line`
3. This means annotations stay anchored to the right location even after lines shift due to edits above the target

**Approach — inline Webview via CodeLens + Decoration:**

VS Code does not natively support "floating panels above a line." The workaround is:

1. Use `vscode.languages.registerCodeLensProvider` to inject a CodeLens above the resolved target line
2. The CodeLens shows the collapsed summary + action buttons as clickable text
3. When expanded, use `vscode.window.createWebviewPanel` as a peek-like panel below the CodeLens (positioned using editor scroll coordination)

**Alternative approach (simpler, v1):**
Use editor decorations (`editor.setDecorations`) with `before` content styling for the collapsed view, and a proper Webview panel that appears adjacent to the editor when expanded. The Webview panel approach is more reliable across VS Code versions.

**Collapsed state (CodeLens line):**
```
  🤖 Intent: Add Redis rate limit counter check before function body  [▼ Expand]  [✓]  [✎]  [✕]
  async function processPayment(req: Request) {
```

**Expanded state (Webview panel, positioned as split below):**
```
┌─ AI Intent for processPayment() ─────────────────────────────┐
│                                                               │
│ WHAT: Add a Redis-backed rate limit check at the entry point  │
│                                                               │
│ HOW: Before any business logic:                               │
│  1. Build key: `ratelimit:${req.ip}:${req.path}`              │
│  2. INCR the key in Redis, set TTL to 900s on first write     │
│  3. If count > 5, throw new RateLimitError(retryAfter)        │
│                                                               │
│ WILL CREATE:                                                  │
│  • RateLimitError class (src/errors/RateLimitError.ts)        │
│                                                               │
│ WILL MODIFY:                                                  │
│  • processPayment() — adds ~8 lines at top of function        │
│                                                               │
│ ⚠️  SIDE EFFECTS:                                             │
│  • RetryQueue.retryFailed() calls this function — if it       │
│    retries from the same IP it may hit the limit. Consider   │
│    a bypass flag for internal retries.                        │
│                                                               │
│ ASSUMPTIONS:                                                  │
│  • Redis client at src/config/redis.ts is already initialised │
│                                                               │
│ QUESTIONS:                                                    │
│  • Should /auth/refresh also be rate limited, or only login?  │
│                                                               │
│ ┌───────────┐  ┌──────────────┐  ┌─────────────────┐        │
│ │ ✓ Approve │  │ ✎ Alter      │  │ ✕ Redirect      │        │
│ └───────────┘  └──────────────┘  └─────────────────┘        │
└───────────────────────────────────────────────────────────────┘
```

**Gutter indicator:**
A small `🤖` icon in the gutter at the target line (using `vscode.window.createTextEditorDecorationType` with `gutterIconPath`). Color changes by annotation status:
- Blue: pending review
- Green: approved
- Orange: in negotiation (altered/question answered)
- Grey: complete (annotation collapsed after execution)

---

### AccordionProvider.ts

Manages the collapse/expand state of all annotations and keyboard navigation between them.

```typescript
class AccordionProvider {
  // Expand a specific annotation
  expandAnnotation(annotationId: string): void
  
  // Collapse a specific annotation
  collapseAnnotation(annotationId: string): void
  
  // Jump to next pending annotation
  navigateToNextPending(): void       // keybind: Alt+] 
  
  // Jump to previous annotation
  navigateToPrevious(): void          // keybind: Alt+[
  
  // Collapse all annotations (when developer wants to see clean code)
  collapseAll(): void                 // keybind: Alt+Shift+C
  
  // Show only pending (hide approved/complete)
  filterToPending(): void
}
```

---

## The Approval Actions

### ✓ Approve

Human clicks Approve (or uses keybind `Alt+A`).

```typescript
async function handleApprove(annotationId: string) {
  annotation.status = 'approved';
  annotation.reviewedAt = Date.now();
  
  // Notify NegotiationController — it will trigger code execution
  negotiationController.onAnnotationApproved(annotation);
  
  // Collapse annotation, show subtle "approved" badge
  accordion.collapseAnnotation(annotationId);
  
  // If more annotations for this task, navigate to next
  // If last annotation, task moves to 'executing'
}
```

### ✎ Alter

Human wants to modify the AI's approach without completely redirecting.

```typescript
async function handleAlter(annotationId: string) {
  // Open inline text input below the annotation
  const alteration = await showAnnotationInput(
    'What should be changed?',
    annotation.detail  // pre-filled with current intent for context
  );
  
  annotation.humanResponse = alteration;
  annotation.status = 'altered';
  
  // Send back to AI for revision
  const revised = await regenerateAnnotation(annotation, alteration);
  
  // Replace annotation in place — human reviews again
  renderAnnotation(revised);
}
```

**Alter input is pre-filled with the current intent text.** The human edits it directly, like a tracked-changes comment on the AI's proposal.

### ✕ Redirect

Human wants to completely change direction. More significant than Alter.

```typescript
async function handleRedirect(annotationId: string) {
  const newDirection = await showAnnotationInput(
    'What should happen instead?',
    ''  // blank — fresh instruction
  );
  
  annotation.humanResponse = newDirection;
  annotation.status = 'redirected';
  
  // Add a session note recording the redirect
  sessionManager.addSessionNote(
    `Redirected annotation for ${annotation.targetFunctionName}: ${newDirection}`,
    'human',
    annotation.taskId
  );
  
  // If redirect changes task scope significantly, ask AI if task list needs updating
  await negotiationController.evaluateRedirectImpact(annotation, newDirection);
}
```

---

## TODO Comment Detection

The developer can add instructions directly in the file using TODO comments. These are treated as live directives.

**Syntax:**
```typescript
// TODO: [wf] don't throw here, return a Result type instead
// TODO: [wf] this needs to handle the case where Redis is down
// TODO: [wf] add a test for the retry bypass flag
```

The `[wf]` tag identifies it as a WaterFree instruction (vs regular project TODOs).

**TodoWatcher monitors for these and:**
1. Converts them to `InsertTask` or `AlterAnnotation` operations depending on context
2. Shows a notification: "WaterFree: TODO instruction detected. [View] [Queue as Task] [Dismiss]"
3. Removes the TODO comment if the human queues it (so the file stays clean)
4. Adds the instruction as a session note

---

## Annotation Persistence

Annotations are stored in `session.json` as part of the Task they belong to. They are not written to source files.

After a task is complete, annotations are retained in the session history (read-only) but no longer rendered in the editor. The developer can view the annotation history from the session panel.

---

## Shadow Code Preview (v2 — not v1)

A future enhancement: after the intent is approved, before final execution, show a diff preview of what the code *will* look like. This gives the developer a chance to see the actual code change before it's applied.

This requires VS Code's diff editor API and a "proposed code" staging area — defer to v2.

---

## Keyboard Shortcuts

| Action | Default Keybind |
|---|---|
| Expand/collapse current annotation | `Alt+I` |
| Approve current annotation | `Alt+A` |
| Alter current annotation | `Alt+E` |
| Redirect | `Alt+R` |
| Next pending annotation | `Alt+]` |
| Previous annotation | `Alt+[` |
| Collapse all annotations | `Alt+Shift+C` |
| Show only pending | `Alt+Shift+P` |
