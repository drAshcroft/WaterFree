# Subsystem 06 — VS Code Extension Scaffold
## WaterFree VS Code Extension

---

## Purpose

This document covers the technical implementation of the VS Code extension shell — the entry point, API registrations, build system, dependency management, and how all subsystems are wired together. This is the document to follow when initialising the repository.

---

## Extension Manifest (package.json)

```json
{
  "name": "pair-protocol",
  "displayName": "WaterFree",
  "description": "Structured AI pair programming. Intent before action.",
  "version": "0.1.0",
  "engines": { "vscode": "^1.85.0" },
  "categories": ["AI", "Programming Languages", "Other"],
  "activationEvents": ["onStartupFinished"],
  "main": "./dist/extension.js",
  
  "contributes": {
    
    "commands": [
      { "command": "waterfree.startSession", "title": "WaterFree: Start Session" },
      { "command": "waterfree.resumeSession", "title": "WaterFree: Resume Session" },
      { "command": "waterfree.endSession", "title": "WaterFree: End Session" },
      { "command": "waterfree.reindex", "title": "WaterFree: Re-index Workspace" },
      { "command": "waterfree.approveAnnotation", "title": "WaterFree: Approve Annotation" },
      { "command": "waterfree.alterAnnotation", "title": "WaterFree: Alter Annotation" },
      { "command": "waterfree.redirectAnnotation", "title": "WaterFree: Redirect" },
      { "command": "waterfree.nextAnnotation", "title": "WaterFree: Next Annotation" },
      { "command": "waterfree.prevAnnotation", "title": "WaterFree: Previous Annotation" },
      { "command": "waterfree.askQuestion", "title": "WaterFree: Ask AI a Question" },
      { "command": "waterfree.takeControl", "title": "WaterFree: I'll Take This" },
      { "command": "waterfree.handBack", "title": "WaterFree: Hand Back to AI" },
      { "command": "waterfree.collapseAll", "title": "WaterFree: Collapse All Annotations" },
      { "command": "waterfree.showSessionNotes", "title": "WaterFree: Show Session Notes" },
      { "command": "waterfree.showTaskBoard", "title": "WaterFree: Show Task Board" },
      { "command": "waterfree.addTask", "title": "WaterFree: Add Task" },
      { "command": "waterfree.whatNext", "title": "WaterFree: What Should I Do Next?" }
    ],
    
    "keybindings": [
      { "command": "waterfree.approveAnnotation", "key": "alt+a", "when": "waterfree.annotationFocused" },
      { "command": "waterfree.alterAnnotation", "key": "alt+e", "when": "waterfree.annotationFocused" },
      { "command": "waterfree.redirectAnnotation", "key": "alt+r", "when": "waterfree.annotationFocused" },
      { "command": "waterfree.nextAnnotation", "key": "alt+]" },
      { "command": "waterfree.prevAnnotation", "key": "alt+[" },
      { "command": "waterfree.askQuestion", "key": "alt+q" },
      { "command": "waterfree.takeControl", "key": "alt+t", "when": "waterfree.sessionActive" },
      { "command": "waterfree.collapseAll", "key": "alt+shift+c", "when": "waterfree.sessionActive" }
    ],
    
    "views": {
      "explorer": [
        {
          "id": "waterfree.planView",
          "name": "WaterFree",
          "when": "waterfree.sessionActive"
        }
      ]
    },
    
    "viewsContainers": {
      "activitybar": [
        {
          "id": "waterfree",
          "title": "WaterFree",
          "icon": "media/icon.svg"
        }
      ]
    },
    
    "configuration": {
      "title": "WaterFree",
      "properties": {
        "waterfree.anthropicApiKey": {
          "type": "string",
          "default": "",
          "description": "Anthropic API key. Alternatively, set ANTHROPIC_API_KEY environment variable."
        },
        "waterfree.planningModel": {
          "type": "string",
          "default": "claude-opus-4-6",
          "enum": ["claude-opus-4-6", "claude-sonnet-4-6"]
        },
        "waterfree.executionModel": {
          "type": "string", 
          "default": "claude-sonnet-4-6",
          "enum": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
        },
        "waterfree.indexing.excludePatterns": {
          "type": "array",
          "default": ["**/node_modules/**", "**/dist/**", "**/.git/**", "**/build/**"]
        },
        "waterfree.indexing.embeddingProvider": {
          "type": "string",
          "enum": ["local", "voyage", "tfidf"],
          "default": "local"
        },
        "waterfree.sideEffects.scanOnSave": { "type": "boolean", "default": true },
        "waterfree.sideEffects.rippleDepth": { "type": "number", "default": 3 },
        "waterfree.ui.showApprovedAnnotations": { "type": "boolean", "default": false },
        "waterfree.ui.autoNavigateToTask": { "type": "boolean", "default": true }
      }
    },
    
    "menus": {
      "view/item/context": [
        {
          "command": "waterfree.alterAnnotation",
          "when": "view == waterfree.planView && viewItem == task"
        }
      ]
    }
  },
  
  "dependencies": {
    "@anthropic-ai/sdk": "^0.26.0",
    "web-tree-sitter": "^0.22.0",
    "tree-sitter-typescript": "^0.21.0",
    "tree-sitter-javascript": "^0.21.0",
    "tree-sitter-python": "^0.21.0",
    "tree-sitter-go": "^0.21.0",
    "tree-sitter-rust": "^0.21.0",
    "@xenova/transformers": "^2.17.0",
    "uuid": "^9.0.0",
    "crypto": "^1.0.1"
  },
  "devDependencies": {
    "@types/vscode": "^1.85.0",
    "@types/node": "^20.0.0",
    "typescript": "^5.3.0",
    "esbuild": "^0.20.0",
    "@vscode/test-electron": "^2.3.0"
  }
}
```

---

## Entry Point (src/extension.ts)

```typescript
import * as vscode from 'vscode';
import { IndexManager } from './indexing/IndexManager';
import { SessionManager } from './session/SessionManager';
import { NegotiationController } from './negotiation/NegotiationController';
import { SideEffectWatcher } from './sideEffects/SideEffectWatcher';
import { PlanSidebar } from './ui/PlanSidebar';
import { DecorationRenderer } from './annotation/DecorationRenderer';
import { TurnManager } from './negotiation/TurnManager';
import { ClaudeClient } from './llm/ClaudeClient';
import { StatusBarManager } from './ui/StatusBarManager';
import { TodoWatcher } from './negotiation/TodoWatcher';

// Singletons — created once, shared across the extension
let indexManager: IndexManager;
let sessionManager: SessionManager;
let negotiationController: NegotiationController;
let sideEffectWatcher: SideEffectWatcher;
let planSidebar: PlanSidebar;
let decorationRenderer: DecorationRenderer;
let turnManager: TurnManager;
let claudeClient: ClaudeClient;
let statusBar: StatusBarManager;
let todoWatcher: TodoWatcher;

export async function activate(context: vscode.ExtensionContext) {
  
  // 1. Initialise core services
  claudeClient = new ClaudeClient(context);
  indexManager = new IndexManager(context);
  turnManager = new TurnManager();
  sessionManager = new SessionManager(context, indexManager, claudeClient, turnManager);
  decorationRenderer = new DecorationRenderer(context);
  sideEffectWatcher = new SideEffectWatcher(indexManager, claudeClient, decorationRenderer);
  negotiationController = new NegotiationController(
    sessionManager, turnManager, claudeClient, 
    decorationRenderer, sideEffectWatcher
  );
  planSidebar = new PlanSidebar(sessionManager, negotiationController);
  statusBar = new StatusBarManager(turnManager, sessionManager);
  todoWatcher = new TodoWatcher(sessionManager, negotiationController);
  
  // 2. Register TreeView for sidebar
  const treeView = vscode.window.createTreeView('waterfree.planView', {
    treeDataProvider: planSidebar,
    showCollapseAll: false,
    dragAndDropController: planSidebar,
  });
  context.subscriptions.push(treeView);
  
  // 3. Register commands
  registerCommands(context);
  
  // 4. Register file watchers
  registerFileWatchers(context);
  
  // 5. Register CodeLens providers (for annotations)
  registerCodeLensProviders(context);
  
  // 6. Check for resumable session
  await checkForResumableSession();
  
  // 7. Start background index if workspace is open
  if (vscode.workspace.workspaceFolders) {
    await indexManager.initialIndex(
      vscode.workspace.workspaceFolders[0].uri.fsPath
    );
  }
  
  statusBar.showReady();
}

function registerCommands(context: vscode.ExtensionContext) {
  context.subscriptions.push(
    vscode.commands.registerCommand('waterfree.startSession', async () => {
      const panel = new PlanningPanel(context, sessionManager, indexManager, claudeClient);
      panel.show();
    }),
    
    vscode.commands.registerCommand('waterfree.approveAnnotation', async () => {
      const focused = decorationRenderer.getFocusedAnnotation();
      if (focused) await negotiationController.onAnnotationApproved(focused);
    }),
    
    vscode.commands.registerCommand('waterfree.alterAnnotation', async () => {
      const focused = decorationRenderer.getFocusedAnnotation();
      if (!focused) return;
      const input = await vscode.window.showInputBox({ 
        prompt: 'What should be changed?',
        value: focused.detail,
        placeHolder: 'Describe the change...'
      });
      if (input) await negotiationController.onAnnotationAltered(focused, input);
    }),
    
    vscode.commands.registerCommand('waterfree.takeControl', async () => {
      await negotiationController.humanTakesControl();
    }),
    
    vscode.commands.registerCommand('waterfree.handBack', async () => {
      await negotiationController.humanHandsBack();
    }),
    
    vscode.commands.registerCommand('waterfree.askQuestion', async () => {
      const question = await vscode.window.showInputBox({
        prompt: 'Ask the AI a question',
        placeHolder: 'e.g. Why did you choose this approach?'
      });
      if (question) await negotiationController.humanAsksQuestion(question);
    })
    // ... other commands
  );
}

function registerFileWatchers(context: vscode.ExtensionContext) {
  // Watch all source files for changes
  const fileWatcher = vscode.workspace.createFileSystemWatcher('**/*.{ts,js,py,go,rs,rb}');
  
  fileWatcher.onDidChange(async (uri) => {
    await indexManager.updateFile(uri.fsPath);
    await sideEffectWatcher.onFileSave(uri.fsPath);
    await todoWatcher.scanFile(uri.fsPath);  // look for [wf] TODOs
  });
  
  fileWatcher.onDidCreate(async (uri) => {
    await indexManager.updateFile(uri.fsPath);
  });
  
  fileWatcher.onDidDelete(async (uri) => {
    await indexManager.removeFile(uri.fsPath);
  });
  
  context.subscriptions.push(fileWatcher);
}

function registerCodeLensProviders(context: vscode.ExtensionContext) {
  // Register for all supported languages
  const languages = ['typescript', 'javascript', 'python', 'go', 'rust', 'ruby'];
  
  for (const language of languages) {
    context.subscriptions.push(
      vscode.languages.registerCodeLensProvider(
        { language },
        decorationRenderer.getCodeLensProvider()
      )
    );
  }
}

async function checkForResumableSession() {
  const session = await sessionManager.resumeSession();
  if (session) {
    const choice = await vscode.window.showInformationMessage(
      `WaterFree: Resume "${session.goalStatement.slice(0, 60)}..."?`,
      'Resume',
      'Start New',
      'Discard'
    );
    
    if (choice === 'Resume') {
      await sessionManager.activateSession(session);
      planSidebar.refresh();
    } else if (choice === 'Start New') {
      await sessionManager.archiveSession(session);
    }
    // 'Discard' or dismissed: do nothing
  }
}

export function deactivate() {
  indexManager?.dispose();
  sessionManager?.saveSession();
  decorationRenderer?.dispose();
  statusBar?.dispose();
}
```

---

## Build System (esbuild.config.js)

```javascript
const esbuild = require('esbuild');

const production = process.argv.includes('--production');

const config = {
  entryPoints: ['src/extension.ts'],
  bundle: true,
  outfile: 'dist/extension.js',
  external: ['vscode'],  // vscode is provided by the extension host
  format: 'cjs',
  platform: 'node',
  target: 'node18',
  sourcemap: !production,
  minify: production,
  
  // Tree-sitter WASM files need special handling
  loader: {
    '.wasm': 'file',
  },
  
  // Copy WASM files to dist
  plugins: [
    {
      name: 'copy-wasm',
      setup(build) {
        build.onEnd(async () => {
          // Copy tree-sitter WASM grammars to dist/
          const grammars = [
            'tree-sitter-typescript.wasm',
            'tree-sitter-javascript.wasm',
            'tree-sitter-python.wasm',
            'tree-sitter-go.wasm',
            'tree-sitter-rust.wasm',
          ];
          // Copy logic here
        });
      }
    }
  ]
};

if (production) {
  esbuild.build(config).catch(() => process.exit(1));
} else {
  esbuild.context(config).then(ctx => ctx.watch());
}
```

---

## ClaudeClient.ts — LLM Integration

```typescript
import Anthropic from '@anthropic-ai/sdk';
import * as vscode from 'vscode';

export class ClaudeClient {
  private client: Anthropic;
  private planningModel: string;
  private executionModel: string;
  
  constructor(context: vscode.ExtensionContext) {
    const apiKey = vscode.workspace.getConfiguration('waterfree').get('anthropicApiKey') as string
      || process.env.ANTHROPIC_API_KEY;
    
    if (!apiKey) {
      vscode.window.showErrorMessage(
        'WaterFree: No Anthropic API key found. Set waterfree.anthropicApiKey in settings or ANTHROPIC_API_KEY env var.'
      );
    }
    
    this.client = new Anthropic({ apiKey });
    this.planningModel = vscode.workspace.getConfiguration('waterfree').get('planningModel') as string;
    this.executionModel = vscode.workspace.getConfiguration('waterfree').get('executionModel') as string;
  }
  
  async generatePlan(prompt: string, indexSummary: string): Promise<Task[]> {
    const response = await this.client.messages.create({
      model: this.planningModel,
      max_tokens: 4096,
      system: SYSTEM_PROMPTS.planning,
      messages: [{ role: 'user', content: prompt }],
      temperature: 0.3,
    });
    
    return JSON.parse(response.content[0].text);
  }
  
  async generateAnnotation(prompt: string): Promise<IntentAnnotation> {
    const response = await this.client.messages.create({
      model: this.executionModel,
      max_tokens: 1024,
      system: SYSTEM_PROMPTS.annotation,
      messages: [{ role: 'user', content: prompt }],
      temperature: 0.2,
    });
    
    return JSON.parse(response.content[0].text);
  }
  
  async streamExecution(prompt: string, onChunk: (chunk: string) => void): Promise<void> {
    const stream = await this.client.messages.create({
      model: this.executionModel,
      max_tokens: 8192,
      system: SYSTEM_PROMPTS.execution,
      messages: [{ role: 'user', content: prompt }],
      stream: true,
      temperature: 0.1,
    });
    
    let fullText = '';
    for await (const event of stream) {
      if (event.type === 'content_block_delta' && event.delta.type === 'text_delta') {
        fullText += event.delta.text;
        onChunk(event.delta.text);
      }
    }
  }
  
  async answerQuestion(question: string, context: QuestionContext): Promise<QuestionAnswer> {
    const response = await this.client.messages.create({
      model: this.executionModel,
      max_tokens: 512,
      system: SYSTEM_PROMPTS.question,
      messages: [{ 
        role: 'user', 
        content: `${JSON.stringify(context)}\n\nQuestion: ${question}` 
      }],
      temperature: 0.4,
    });
    
    return JSON.parse(response.content[0].text);
  }
}

// ── TaskCommand — embedded task store operations ──────────────────────────────
//
// Every AI response may include an optional `taskCommands` array alongside its
// primary payload. The extension processes these commands against the TaskStore
// after handling the primary response. This lets the AI manage the task backlog
// as a side-effect of planning, annotating, executing, or answering questions.
//
// Command schema (discriminated union on `op`):

type TaskCommand =
  | {
      op: 'add-task';
      task: Partial<Task>;           // at minimum: title, description, targetCoord, priority
    }
  | {
      op: 'update-task';
      taskId: string;
      patch: Partial<Task>;          // only the fields to change
    }
  | {
      op: 'split-task';
      taskId: string;                // the task being replaced
      subtasks: Partial<Task>[];     // the tasks to insert in its place
      reason: string;                // why the split was needed
    }
  | {
      op: 'block-task';
      taskId: string;
      blockedReason: string;
      blockedBy?: string;            // taskId of the blocking task if known
    }
  | {
      op: 'add-note';
      content: string;
      relatedTaskId?: string;
    };

// All AI responses use this envelope. Primary payload is in the named field,
// task commands are always optional alongside it.
interface AIResponse<T> {
  payload: T;
  taskCommands?: TaskCommand[];
}

// Processor — called after every AI response:
class TaskCommandProcessor {
  async process(commands: TaskCommand[], session: PlanDocument): Promise<void> {
    for (const cmd of commands) {
      switch (cmd.op) {
        case 'add-task': {
          const task = await taskStore.addTask(cmd.task);
          // Notify sidebar: new task added
          planSidebar.refresh();
          vscode.window.showInformationMessage(
            `AI added task: "${task.title}"`,
            'View'
          ).then(c => { if (c === 'View') planSidebar.focusTask(task.id); });
          break;
        }
        case 'update-task': {
          await taskStore.updateTask(cmd.taskId, cmd.patch);
          planSidebar.refresh();
          break;
        }
        case 'split-task': {
          // Human must approve a split — it changes the plan shape
          const choice = await vscode.window.showInformationMessage(
            `AI wants to split task into ${cmd.subtasks.length} subtasks: ${cmd.reason}`,
            'Approve Split',
            'Reject'
          );
          if (choice === 'Approve Split') {
            await taskStore.deleteTask(cmd.taskId);
            for (const sub of cmd.subtasks) {
              await taskStore.addTask(sub);
            }
            planSidebar.refresh();
          }
          break;
        }
        case 'block-task': {
          await taskStore.updateTask(cmd.taskId, {
            blockedReason: cmd.blockedReason,
          });
          if (cmd.blockedBy) {
            const dep: TaskDependency = { taskId: cmd.blockedBy, type: 'blocks' };
            const task = taskStore.getTask(cmd.taskId);
            await taskStore.updateTask(cmd.taskId, {
              dependsOn: [...(task?.dependsOn ?? []), dep],
            });
          }
          planSidebar.refresh();
          break;
        }
        case 'add-note': {
          session.addSessionNote(cmd.content, 'ai', cmd.relatedTaskId);
          break;
        }
      }
    }
  }
}

const SYSTEM_PROMPTS = {
  // Injected into every prompt — the AI always knows about task commands
  _taskCommandSchema: `
TASK STORE COMMANDS
You have access to the shared task store. You may include a "taskCommands" array
in your JSON response alongside your primary payload to manipulate tasks.

Available operations:

  { "op": "add-task", "task": { ...Partial<Task> } }
    → Add a new task to the backlog. Always include: title, description,
      targetCoord (file + method/class), priority (P0/P1/P2/P3/spike).
    → Use when you discover scope that was not in the original plan.

  { "op": "update-task", "taskId": "...", "patch": { ...fields } }
    → Update any fields on an existing task. Use for status, notes, priority changes.

  { "op": "split-task", "taskId": "...", "subtasks": [...], "reason": "..." }
    → Replace a task with subtasks. Human must approve. Use when a task is
      larger than originally understood. Requires a clear reason.

  { "op": "block-task", "taskId": "...", "blockedReason": "...", "blockedBy": "taskId?" }
    → Mark a task blocked. Use when you discover a prerequisite that is not met.

  { "op": "add-note", "content": "...", "relatedTaskId": "?" }
    → Add a session note (decision log, observation). Always use for significant
      discoveries, even if no task change is needed.

Rules:
- Never emit task commands that change tasks outside the current session's scope
  without flagging it as a WARNING in the note
- "split-task" always requires human approval — never assume it will be accepted
- Prefer "add-note" over silence when you discover something unexpected
`,

  planning: `You are an expert software developer starting a pair programming session.
You have been given a codebase index and a goal. Your job is to create a clear,
ordered implementation plan. You must explain your reasoning, surface uncertainties,
and ask questions before planning rather than making assumptions.

For each task you MUST provide:
- targetCoord: { file, class?, method?, line?, anchorType }
- contextCoords: locations the developer needs to see but not edit while on this task
- priority: P0 (blocker) | P1 (critical path) | P2 (should do) | P3 (backlog) | spike
- dependsOn: array of { taskId, type: 'blocks' | 'informs' | 'shares-file' }
- owner: { type: 'human' | 'agent' | 'unassigned', name: string }
- taskType: 'impl' | 'test' | 'spike' | 'review' | 'refactor'
- estimatedMinutes: your honest estimate (not shown as a promise)

Respond with valid JSON: { "payload": Task[], "taskCommands": TaskCommand[] }
Use taskCommands to add backlog items (P3) that you notice but are out of scope,
and to add notes for any uncertainty or question that should be recorded.

${TASK_COMMAND_SCHEMA_PLACEHOLDER}`,

  annotation: `You are in the intent declaration phase of pair programming.
You must describe what you plan to do BEFORE doing it.
Be specific about what will change and what the side effects are.
Surface all assumptions. Ask questions if uncertain.
Never write code in this phase.

Your annotation MUST include:
- targetCoord: the exact CodeCoord (inherit from task, narrow to specific method line if possible)
- contextCoords: any additional locations the developer should see while reviewing

Respond with valid JSON: { "payload": IntentAnnotation, "taskCommands": TaskCommand[] }
Use taskCommands to:
- split-task if this task is bigger than planned (requires human approval)
- add-task for any follow-up work you discover is needed
- block-task if you discover a prerequisite that is not met
- add-note for any significant observation

${TASK_COMMAND_SCHEMA_PLACEHOLDER}`,

  execution: `You are in execution mode. All intent has been approved by the human developer.
Write exactly what was described in the approved annotations.
Do not add extra features. Do not refactor adjacent code unless specified.

Respond with valid JSON: { "payload": CodeEdit, "taskCommands": TaskCommand[] }
Use taskCommands to:
- add-task for any follow-up work or TODOs you needed to leave in the code
- add-note to record any implementation decision that deviated from the annotation
  (even minor ones — the session log is the truth record)
- update-task to set actualMinutes on the completed task if you can estimate it

${TASK_COMMAND_SCHEMA_PLACEHOLDER}`,

  question: `You are answering a question from your pair programming partner.
Be concise and direct. Flag if the answer should change the plan.

Respond with valid JSON:
{ "payload": { "text": "...", "planImpact": "..." | null }, "taskCommands": TaskCommand[] }
Use taskCommands to record the Q&A as a session note, and to flag any task
that should change based on the answer.

${TASK_COMMAND_SCHEMA_PLACEHOLDER}`,
};
```

---

## ContextBuilder.ts

Assembles the right context for each type of AI call without overflowing the context window.

```typescript
export class ContextBuilder {
  
  // For planning: high-level overview of the whole codebase
  buildPlanningContext(goalStatement: string): string {
    const summary = indexManager.getIndexSummary();
    
    return `
CODEBASE OVERVIEW:
Files: ${summary.fileCount} (${Object.entries(summary.languageBreakdown).map(([l,c]) => `${l}: ${c}`).join(', ')})
Entry points: ${summary.entryPoints.join(', ')}
Key modules: ${summary.topLevelModules.map(m => m.name).join(', ')}
Existing TODOs: ${summary.existingTodos.length}

MODULE SUMMARIES:
${summary.topLevelModules.map(m => `• ${m.name}: ${m.description}`).join('\n')}

GOAL: ${goalStatement}
    `.trim();
  }
  
  // For annotation: focused context on the specific function being annotated
  buildAnnotationContext(task: Task): string {
    const coord = task.targetCoord;
    const fnContext = indexManager.getFunctionContext(
      coord.method ?? coord.class ?? '',
      coord.file
    );

    // Top K semantically similar functions from the codebase
    const similar = indexManager.findSimilarFunctions(task.description, 3);

    // Resolve context coords to code snippets the AI should reference
    const contextSnippets = (task.contextCoords ?? []).map(c => ({
      label: `${c.file}${c.method ? `::${c.method}` : ''}`,
      code: indexManager.getFunctionContext(c.method ?? '', c.file).code,
    }));

    return `
TASK: ${task.description}
PRIORITY: ${task.priority}  PHASE: ${task.phase ?? 'none'}  TYPE: ${task.taskType}

TARGET COORD: ${coord.file} :: ${coord.class ? `${coord.class}.` : ''}${coord.method ?? '(file level)'}${coord.line ? ` (line ${coord.line})` : ''}

CURRENT CODE AT TARGET:
\`\`\`
${fnContext.code}
\`\`\`

CONTEXT COORDS (read-only — these are open in split view for the developer):
${contextSnippets.map(c => `--- ${c.label} ---\n${c.code}`).join('\n\n')}

DIRECT DEPENDENCIES:
${fnContext.dependencies.map(d => `• ${d.name} — ${d.filePath}:${d.line}`).join('\n')}

CALLERS:
${fnContext.callers.map(c => `• ${c.name} — ${c.filePath}:${c.line}`).join('\n')}

SIMILAR EXISTING PATTERNS:
${similar.map(s => `• ${s.name} (${s.filePath})\n  ${s.snippet}`).join('\n\n')}

SESSION CONTEXT:
Goal: ${session.goalStatement}
Completed tasks: ${session.getCompletedTasks().map(t => `[${t.priority}] ${t.title}`).join(', ')}
    `.trim();
  }

  // For execution: approved annotations + target code
  buildExecutionContext(task: Task): string {
    const approved = task.annotations.filter(a => a.status === 'approved');
    const fileContents = approved.map(a => ({
      file: a.targetCoord.file,
      content: readFileSync(a.targetCoord.file, 'utf-8'),
    }));
    
    return `
APPROVED INTENT:
${approved.map(a => `
TARGET: ${a.targetCoord.file}::${a.targetCoord.class ? `${a.targetCoord.class}.` : ''}${a.targetCoord.method ?? '(file level)'}${a.targetCoord.line ? ` ~line ${a.targetCoord.line}` : ''}
WHAT: ${a.summary}
HOW: ${a.detail}
APPROACH: ${a.approach}
CREATES: ${a.willCreate.join(', ')}
MODIFIES: ${a.willModify.join(', ')}
DELETES: ${a.willDelete.join(', ')}
`).join('\n')}

CURRENT FILE CONTENTS:
${fileContents.map(f => `--- ${f.file} ---\n${f.content}`).join('\n\n')}
    `.trim();
  }
}
```

---

## Testing Strategy

**Unit tests** (using `@vscode/test-electron`):
- `IndexManager`: parse known fixtures, verify graph structure
- `TurnManager`: state machine transitions
- `RippleDetector`: known dependency graphs, verify correct nodes identified

**Integration tests:**
- Full session lifecycle against a fixture project
- Annotation generation → approval → execution on a real small codebase
- Side effect detection on a known change

**Fixture projects** (in `test/fixtures/`):
- `simple-express-api/` — a minimal Express app with auth, suitable for most test scenarios
- `python-cli/` — a small Python CLI for Python parsing tests

---

## Development Workflow

```bash
# Install dependencies
npm install

# Watch mode (incremental build)
npm run watch

# Launch extension in dev host
# Press F5 in VS Code with the repo open

# Run tests
npm test

# Package for marketplace
npm run package   # creates .vsix
```

---

## .waterfree/ Directory

Add to your project's `.gitignore`:
```
.waterfree/
```

Structure:
```
.waterfree/
├── index.json          # full parsed index (auto-generated)
├── graph.json          # dependency graph (auto-generated)
├── embeddings.bin      # vector embeddings (auto-generated)
├── index.meta.json     # hashes + timestamps for cache invalidation
├── tasks.db            # persistent cross-session task store (see Subsystem 07)
└── sessions/
    ├── current.json    # active session (PlanDocument)
    └── archive/        # completed sessions (for reference)
        └── 2024-01-15-add-rate-limiting.json
```

`tasks.db` is the durable backlog — P3 items, deferred tasks, and auto-generated tasks from `// TODO: [wf]` comments that are not tied to a specific session. Session tasks live in `current.json`; cross-session tasks live here.

---

## Known Technical Challenges

**1. Tree-sitter WASM in extension host**
Tree-sitter runs as WASM in Node.js. Extension host has restrictions on WASM loading path. Solution: use `web-tree-sitter` which handles this correctly, and copy WASM files to `dist/` during build.

**2. @xenova/transformers size**
The local embedding model is ~30MB. This is large for a VS Code extension. Options:
- Ship it (acceptable for a developer tool)
- Download on first use with progress indicator
- Make it opt-in, default to TF-IDF until user enables local embeddings

**3. CodeLens positioning for annotations**
CodeLens renders above a line, which is correct. But the expanded view needs to feel like it's "floating" — not a separate panel. Best approach: use the `vscode.window.createWebviewPanel` in a custom editor column positioned to be adjacent. Alternative: render expansion inline using editor decorations with `before`/`after` content, accepting display limitations.

**4. Worker threads in extension host**
Indexing must run off the main thread. Use `worker_threads` (Node.js built-in). The extension host supports this in VS Code 1.85+. Ensure the worker file is included in `dist/`.

**5. Large codebases (>50,000 files)**
Initial indexing of very large repos will be slow. Mitigations:
- Index only files in `src/` and similar directories by default
- Show progress with cancellation option
- Stream index updates to UI as files complete (don't wait for full index)
