# Subsystem 04 — Negotiation Protocol & Turn Management
## WaterFree VS Code Extension

---

## Purpose

The negotiation layer is the protocol engine. It enforces the turn-taking structure that keeps the experience from devolving into either "AI does everything" or "AI never does anything useful." It manages who is acting at any given moment, how transitions happen, and what the rules are for each party.

This is the layer that gives the session its rhythm.

---

## The Core Protocol

WaterFree uses a structured alternating protocol, but it is not strictly turn-based. The human can interject at any point. The AI cannot act unless it is in a designated state.

**Valid AI action states:**
- `PLANNING` — AI may generate the plan document
- `ANNOTATING` — AI may write intent annotations (no code)
- `EXECUTING` — AI may write code (only after annotation is approved)
- `ANSWERING` — AI may respond to a human question
- `SCANNING` — AI may run side effect scan (passive, no edits)

**In all other states, the AI produces no output and makes no changes.**

---

## Components

### TurnManager.ts

The state machine for the protocol.

```typescript
type AIState = 
  | 'idle'
  | 'planning'
  | 'annotating'
  | 'awaiting_review'     // annotation written, waiting for human
  | 'executing'
  | 'scanning'
  | 'answering'
  | 'awaiting_redirect'   // human redirected, AI reformulating

type HumanState =
  | 'reviewing_plan'
  | 'reviewing_annotation'
  | 'coding'              // human is coding, AI is dormant
  | 'asking_question'
  | 'editing_task_list'

class TurnManager {
  getAIState(): AIState
  getHumanState(): HumanState
  
  // Transitions
  async transitionTo(state: AIState, reason: string): Promise<void>
  
  // Human-triggered state changes
  async humanStartsReview(): Promise<void>
  async humanApprovesAnnotation(annotationId: string): Promise<void>
  async humanAltersAnnotation(annotationId: string, alteration: string): Promise<void>
  async humanRedirects(annotationId: string, newDirection: string): Promise<void>
  async humanAsksQuestion(question: string): Promise<void>
  async humanTakesControl(): Promise<void>   // human wants to code themselves
  async humanHandsBack(): Promise<void>      // human done, AI can resume
  
  // AI-triggered state changes
  async aiFinishedAnnotating(taskId: string): Promise<void>
  async aiFinishedExecuting(taskId: string): Promise<void>
  async aiHasQuestion(question: string): Promise<void>
  
  // Status bar shows current state
  onStateChanged: EventEmitter<{ aiState: AIState, humanState: HumanState }>
}
```

**State transition diagram:**
```
                    ┌─────────────┐
        session     │             │
        starts ────▶│   idle      │
                    │             │
                    └──────┬──────┘
                           │ human submits planning form
                           ▼
                    ┌─────────────┐
                    │  planning   │◀─── human edits plan ──┐
                    │             │                         │
                    └──────┬──────┘                         │
                           │ human approves plan            │
                           ▼                                │
                    ┌─────────────┐                         │
              ┌────▶│ annotating  │                         │
              │     │             │                         │
              │     └──────┬──────┘                         │
              │            │ AI writes annotation           │
              │            ▼                                │
              │     ┌──────────────┐                        │
              │     │  awaiting    │                        │
              │     │  review      │                        │
              │     └──────┬───────┘                        │
              │            │                                │
              │    ┌───────┴──────────┐                     │
              │    │                  │                     │
              │ alter/             approve                  │
              │ redirect              │                     │
              │    │                  ▼                     │
              │    │           ┌─────────────┐              │
              │    │           │  executing  │              │
              │    │           │             │              │
              │    │           └──────┬──────┘              │
              │    │                  │ code written         │
              │    │                  ▼                     │
              │    │           ┌─────────────┐              │
              │    │           │  scanning   │              │
              │    │           │             │              │
              │    │           └──────┬──────┘              │
              │    │                  │                     │
              └────┴──────────────────┘ ──── next task ─────┘
```

---

### NegotiationController.ts

Handles the business logic of each negotiation event.

```typescript
class NegotiationController {
  
  async onAnnotationApproved(annotation: IntentAnnotation): Promise<void> {
    // 1. Mark annotation approved
    // 2. Check if all annotations for this task are approved
    // 3. If yes: transition to executing
    // 4. If no: navigate to next annotation for this task
    
    const task = session.getTask(annotation.taskId);
    const allApproved = task.annotations.every(a => a.status === 'approved');
    
    if (allApproved) {
      await turnManager.transitionTo('executing', 'all annotations approved');
      await this.executeTask(task);
    } else {
      accordion.navigateToNextPending();
    }
  }
  
  async onAnnotationAltered(annotation: IntentAnnotation, alteration: string): Promise<void> {
    // 1. Send alteration back to AI with original annotation as context
    // 2. AI regenerates annotation
    // 3. New annotation replaces old in place
    // 4. Human reviews again
    
    await turnManager.transitionTo('annotating', 'human altered annotation');
    const revised = await claudeClient.reviseAnnotation(annotation, alteration);
    annotationRenderer.replaceAnnotation(annotation.id, revised);
    await turnManager.transitionTo('awaiting_review', 'annotation revised');
  }
  
  async onAnnotationRedirected(annotation: IntentAnnotation, newDirection: string): Promise<void> {
    // 1. Record the redirect in session notes
    // 2. Check if redirect affects the broader task
    // 3. If redirect changes task scope: propose task list update
    // 4. AI re-annotates with new direction
    
    const impactAssessment = await claudeClient.assessRedirectImpact(
      annotation, 
      newDirection, 
      session.plan.tasks
    );
    
    if (impactAssessment.requiresTaskListChange) {
      await this.proposeTaskListUpdate(impactAssessment.proposedChanges);
    }
    
    await turnManager.transitionTo('annotating', 'redirect received');
    const newAnnotation = await claudeClient.annotateFromRedirect(annotation, newDirection);
    annotationRenderer.replaceAnnotation(annotation.id, newAnnotation);
  }
  
  async executeTask(task: Task): Promise<void> {
    // 1. Build execution context (all approved annotations + full task context)
    // 2. Stream code changes from AI
    // 3. Apply changes via WorkspaceEdit
    // 4. Show diff to human (optional — configurable)
    // 5. Trigger side effect scan
    // 6. Mark task complete
    
    const context = session.buildTaskContext(task.id);
    const executionPrompt = promptTemplates.buildExecutionPrompt(task, context);
    
    const edit = new vscode.WorkspaceEdit();
    
    // Stream the response and build the edit
    await claudeClient.streamExecution(executionPrompt, (chunk) => {
      // Parse structured response into file edits
      this.applyEditChunk(edit, chunk);
    });
    
    // Apply all changes atomically
    await vscode.workspace.applyEdit(edit);
    
    // Trigger side effect scan
    sideEffectWatcher.scan(task);
    
    await turnManager.transitionTo('scanning', 'code executed');
    await session.completeTask(task.id, 'Executed successfully');
  }
  
  async evaluateRedirectImpact(annotation: IntentAnnotation, redirect: string): Promise<void> {
    // Quick assessment — does this redirect change the plan?
    const assessment = await claudeClient.quickAssess(
      `Does this redirect require changing the task list? Redirect: "${redirect}". 
       Current task: ${annotation.taskId}. 
       Remaining tasks: ${JSON.stringify(session.getRemainingTasks())}`
    );
    
    if (assessment.planChangeRequired) {
      vscode.window.showInformationMessage(
        `This redirect may require updating the plan. ${assessment.reason}`,
        'Review Plan',
        'Ignore'
      ).then(choice => {
        if (choice === 'Review Plan') {
          sidebar.highlightAffectedTasks(assessment.affectedTaskIds);
        }
      });
    }
  }
}
```

---

### HumanTakesControl

A first-class action: the human wants to code something themselves without AI involvement.

```typescript
async function humanTakesControl(): Promise<void> {
  // Called via command palette: "WaterFree: I'll take this"
  // OR by clicking "Take Over" button in annotation panel
  
  await turnManager.transitionTo('idle', 'human taking control');
  
  // Collapse all annotations
  accordion.collapseAll();
  
  // Status bar: "WaterFree: You have control | [Hand Back]"
  statusBar.showHumanControl();
  
  // Start watching for changes
  changeWatcher.startTracking();
}

async function humanHandsBack(): Promise<void> {
  // Human clicks "Hand Back" or runs command
  
  const changes = changeWatcher.getChanges();
  
  // Brief AI acknowledgment of what the human did
  const acknowledgment = await claudeClient.acknowledgeHumanChanges(changes, session.currentTask);
  
  // Show a non-blocking notification with the acknowledgment
  // e.g. "Got it — you've added the bypass flag to the retry logic. Ready to continue."
  statusBar.showNotification(acknowledgment.summary);
  
  // Update session context with human's changes
  await indexManager.updateFiles(changes.map(c => c.filePath));
  
  // Resume protocol
  await resumeFromHumanEdit(changes);
}
```

This is important: when the human codes something directly, the AI reads what was written before continuing. It doesn't assume the original plan is still fully valid.

---

### QuestionProtocol

Both parties can ask questions. Questions are structured, not open chat.

**AI asks a question:**
```typescript
async function aiAsksQuestion(question: string, context: string, taskId: string): Promise<void> {
  // Rendered as a non-blocking panel below the relevant annotation
  // NOT a popup, NOT a modal
  // Human can answer immediately or dismiss to answer later
  
  const questionPanel = new QuestionPanel({
    author: 'ai',
    question,
    context,          // why the AI is asking (one sentence)
    taskId,
    options: question.isBooleanQuestion 
      ? ['Yes', 'No', 'Explain more'] 
      : null,         // free text if not a yes/no question
  });
  
  questionPanel.onAnswer(async (answer) => {
    session.addSessionNote(`AI asked: ${question} | Human answered: ${answer}`, 'ai', taskId);
    await claudeClient.receiveAnswer(answer, taskId);
    questionPanel.dismiss();
  });
  
  questionPanel.onDefer(() => {
    // Mark question as deferred
    // Show badge on task in sidebar
    // Human can answer from sidebar context menu
  });
}
```

**Human asks the AI:**
```typescript
// Via keybind Alt+Q or command palette
async function humanAsksQuestion(question: string): Promise<void> {
  // AI answers in a non-modal panel
  // Answer is added to session notes
  // If answer reveals something that changes the plan, AI flags it
  
  const answer = await claudeClient.answerQuestion(question, {
    currentTask: session.getCurrentTask(),
    indexContext: indexManager.getTaskContext(session.getCurrentTask().id),
    planContext: session.plan,
  });
  
  const answerPanel = new QuestionPanel({
    author: 'human',
    question,
    answer: answer.text,
    planImpact: answer.planImpact,  // null if no impact, else description
  });
  
  if (answer.planImpact) {
    answerPanel.showPlanImpactWarning(answer.planImpact);
  }
}
```

---

## Execution: How Code Gets Written

When a task moves to `executing`, the AI writes code via VS Code's `WorkspaceEdit` API.

**Execution prompt template:**
```
You are now in EXECUTION mode. All intent has been approved.
You must now write the exact code described in the following approved annotations.

Do NOT deviate from the approved intent.
Do NOT add extra features not mentioned.
Do NOT refactor adjacent code unless it was in the intent.

APPROVED ANNOTATIONS:
[IntentAnnotation[]]

FULL CONTEXT:
[TaskContext]

EXISTING CODE AT TARGET:
[current file content]

Respond with a JSON CodeEdit object:
{
  "edits": [
    {
      "file": "relative/path/to/file.ts",
      "operation": "insert" | "replace" | "delete" | "create",
      "startLine": N,
      "endLine": N,        // for replace/delete
      "content": "...",    // for insert/replace/create
      "explanation": "..."  // one line, shown in status bar
    }
  ]
}
```

**Atomicity:** All edits in the CodeEdit response are applied as a single `WorkspaceEdit` transaction. If any edit fails (e.g. file has changed since annotation), the entire transaction is rolled back and the human is notified.

---

## Status Bar

The status bar item (bottom of VS Code) shows the current state:

```
 🤖 WaterFree: Awaiting review  (Task 3/7)   [?] [⏸]
 🤖 WaterFree: AI executing...  (Task 3/7)   [⏸]
 🤖 WaterFree: You have control              [Hand Back]
 🤖 WaterFree: Scanning effects (Task 3/7)   [View]
 🤖 WaterFree: 2 questions pending           [View]
```

The status bar is always visible and is the quick-glance health indicator for the session.
