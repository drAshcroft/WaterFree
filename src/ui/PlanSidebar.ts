/**
 * PlanSidebar — VS Code webview sidebar showing the current session plus a
 * persistent prompt composer for starting a new session.
 */

import * as path from "path";
import * as vscode from "vscode";

export type CoordAnchorType = "create-at" | "modify" | "delete" | "read-only-context";

export interface CodeCoord {
  file: string;
  class?: string;
  method?: string;
  line?: number;
  anchorType: CoordAnchorType;
}

export type TaskPriority = "P0" | "P1" | "P2" | "P3" | "spike";
export type TaskType = "impl" | "test" | "spike" | "review" | "refactor";

export interface TaskOwner {
  type: "human" | "agent" | "unassigned";
  name: string;
}

export interface AnnotationData {
  id: string;
  summary: string;
  detail?: string;
  approach?: string;
  targetCoord: CodeCoord;
  contextCoords?: CodeCoord[];
  willCreate?: string[];
  willModify?: string[];
  willDelete?: string[];
  sideEffectWarnings?: string[];
  questionsBeforeProceeding?: string[];
  status: "pending" | "approved" | "altered" | "redirected";
}

export interface TaskData {
  id: string;
  title: string;
  description: string;
  rationale?: string;
  targetCoord: CodeCoord;
  contextCoords?: CodeCoord[];
  priority: TaskPriority;
  phase?: string;
  owner: TaskOwner;
  taskType: TaskType;
  blockedReason?: string;
  status: "pending" | "annotating" | "negotiating" | "executing" | "complete" | "skipped";
  annotations: AnnotationData[];
}

export interface PlanData {
  id: string;
  goalStatement: string;
  status: string;
  tasks: TaskData[];
}

export interface BacklogTaskData {
  id: string;
  title: string;
  priority: TaskPriority;
  phase?: string;
  owner: TaskOwner;
  targetCoord: CodeCoord;
  blockedReason?: string;
  status: "pending" | "annotating" | "negotiating" | "executing" | "complete" | "skipped";
}

export interface BacklogSummaryData {
  nextTask: BacklogTaskData | null;
  readyTasks: BacklogTaskData[];
  totalReady: number;
}

export type SidebarAction =
  | { type: "startSession"; goal: string; persona: string }
  | { type: "openTask"; taskId: string }
  | { type: "generateAnnotation"; taskId: string }
  | { type: "showAnnotation"; taskId: string; annotationId?: string }
  | { type: "approveAnnotation"; annotationId: string }
  | { type: "alterAnnotation"; taskId: string; annotationId: string; feedback: string }
  | { type: "redirectTask"; taskId: string; instruction: string }
  | { type: "skipTask"; taskId: string }
  | { type: "buildKnowledge" }
  | { type: "addKnowledgeRepo" }
  | { type: "snippetizeSymbol"; symbol: string; context: string }
  | { type: "pushDebugToAgent"; intent: string; stopReason: string };

type SidebarViewState = {
  plan: PlanData | null;
  backlogSummary: BacklogSummaryData;
  busyMessage: string | null;
  checkingForSession: boolean;
  debugActive: boolean;
  debugLocation: string;
};

export function getTaskTargetPath(task: TaskData, workspacePath: string): string | null {
  const file = task.targetCoord.file?.trim();
  if (!file) {
    return null;
  }
  return path.isAbsolute(file) ? path.normalize(file) : path.join(workspacePath, file);
}

export function getTaskTargetLine(task: TaskData): number {
  return clampToZeroBased(task.targetCoord.line);
}

export function getAnnotationTargetLine(annotation: AnnotationData, task: TaskData): number {
  return clampToZeroBased(annotation.targetCoord.line ?? task.targetCoord.line);
}

export function getTaskLocationLabel(task: TaskData): string {
  return formatCoord(task.targetCoord);
}

export class PlanSidebarProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  private readonly _actionEmitter = new vscode.EventEmitter<SidebarAction>();
  private readonly _disposables: vscode.Disposable[] = [];

  private _view: vscode.WebviewView | null = null;
  private _state: SidebarViewState = {
    plan: null,
    backlogSummary: {
      nextTask: null,
      readyTasks: [],
      totalReady: 0,
    },
    busyMessage: null,
    checkingForSession: true,
    debugActive: false,
    debugLocation: "",
  };

  readonly onDidTriggerAction = this._actionEmitter.event;

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this._view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
    };
    webviewView.webview.html = this._getHtml(webviewView.webview);

    const receiveDisposable = webviewView.webview.onDidReceiveMessage((message: unknown) => {
      this._handleMessage(message);
    });
    const disposeDisposable = webviewView.onDidDispose(() => {
      if (this._view === webviewView) {
        this._view = null;
      }
    });

    this._disposables.push(receiveDisposable, disposeDisposable);
    this._postState();
  }

  update(plan: PlanData | null): void {
    this._state = { ...this._state, plan };
    this._postState();
  }

  setBacklogSummary(backlogSummary: BacklogSummaryData): void {
    this._state = { ...this._state, backlogSummary };
    this._postState();
  }

  setBusyMessage(message: string | null): void {
    this._state = { ...this._state, busyMessage: message };
    this._postState();
  }

  setCheckingForSession(checkingForSession: boolean): void {
    this._state = { ...this._state, checkingForSession };
    this._postState();
  }

  setDebugState(debugActive: boolean, debugLocation: string): void {
    this._state = { ...this._state, debugActive, debugLocation };
    this._postState();
  }

  clearComposer(): void {
    void this._view?.webview.postMessage({ type: "clearComposer" });
  }

  prefillSnippetize(symbol: string): void {
    void this._view?.webview.postMessage({ type: "prefillSnippetize", symbol });
  }

  dispose(): void {
    for (const disposable of this._disposables) {
      disposable.dispose();
    }
    this._disposables.length = 0;
    this._actionEmitter.dispose();
  }

  private _postState(): void {
    if (!this._view) {
      return;
    }
    void this._view.webview.postMessage({
      type: "state",
      state: this._state,
    });
  }

  private _handleMessage(message: unknown): void {
    if (!isRecord(message) || typeof message.type !== "string") {
      return;
    }

    switch (message.type) {
      case "startSession":
        if (typeof message.goal === "string" && message.goal.trim()) {
          this._actionEmitter.fire({
            type: "startSession",
            goal: message.goal.trim(),
            persona: typeof message.persona === "string" ? message.persona : "architect",
          });
        }
        return;
      case "openTask":
        if (typeof message.taskId === "string") {
          this._actionEmitter.fire({ type: "openTask", taskId: message.taskId });
        }
        return;
      case "generateAnnotation":
        if (typeof message.taskId === "string") {
          this._actionEmitter.fire({ type: "generateAnnotation", taskId: message.taskId });
        }
        return;
      case "showAnnotation":
        if (typeof message.taskId === "string") {
          this._actionEmitter.fire({
            type: "showAnnotation",
            taskId: message.taskId,
            annotationId: typeof message.annotationId === "string" ? message.annotationId : undefined,
          });
        }
        return;
      case "approveAnnotation":
        if (typeof message.annotationId === "string") {
          this._actionEmitter.fire({ type: "approveAnnotation", annotationId: message.annotationId });
        }
        return;
      case "alterAnnotation":
        if (
          typeof message.taskId === "string" &&
          typeof message.annotationId === "string" &&
          typeof message.feedback === "string" &&
          message.feedback.trim()
        ) {
          this._actionEmitter.fire({
            type: "alterAnnotation",
            taskId: message.taskId,
            annotationId: message.annotationId,
            feedback: message.feedback.trim(),
          });
        }
        return;
      case "redirectTask":
        if (
          typeof message.taskId === "string" &&
          typeof message.instruction === "string" &&
          message.instruction.trim()
        ) {
          this._actionEmitter.fire({
            type: "redirectTask",
            taskId: message.taskId,
            instruction: message.instruction.trim(),
          });
        }
        return;
      case "skipTask":
        if (typeof message.taskId === "string") {
          this._actionEmitter.fire({ type: "skipTask", taskId: message.taskId });
        }
        return;
      case "buildKnowledge":
        this._actionEmitter.fire({ type: "buildKnowledge" });
        return;
      case "addKnowledgeRepo":
        this._actionEmitter.fire({ type: "addKnowledgeRepo" });
        return;
      case "snippetizeSymbol":
        if (typeof message.symbol === "string" && message.symbol.trim()) {
          this._actionEmitter.fire({
            type: "snippetizeSymbol",
            symbol: message.symbol.trim(),
            context: typeof message.context === "string" ? message.context.trim() : "",
          });
        }
        return;
      case "pushDebugToAgent":
        if (typeof message.intent === "string") {
          this._actionEmitter.fire({
            type: "pushDebugToAgent",
            intent: message.intent.trim(),
            stopReason: typeof message.stopReason === "string" ? message.stopReason : "other",
          });
        }
        return;
    }
  }

  private _getHtml(webview: vscode.Webview): string {
    const nonce = getNonce();
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource} 'unsafe-inline'`,
      `script-src 'nonce-${nonce}'`,
    ].join("; ");

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="${csp}" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    :root {
      color-scheme: light dark;
      --panel: color-mix(in srgb, var(--vscode-sideBar-background) 92%, var(--vscode-textLink-foreground) 8%);
      --panel-strong: color-mix(in srgb, var(--vscode-sideBar-background) 82%, var(--vscode-editorWidget-border) 18%);
      --accent: var(--vscode-textLink-foreground);
      --accent-strong: color-mix(in srgb, var(--vscode-textLink-foreground) 70%, black 30%);
      --muted: var(--vscode-descriptionForeground);
      --border: color-mix(in srgb, var(--vscode-editorWidget-border) 80%, transparent 20%);
      --warning: var(--vscode-inputValidation-warningForeground);
      --success: var(--vscode-testing-iconPassed);
      --danger: var(--vscode-errorForeground);
      --shadow: 0 10px 30px rgba(0, 0, 0, 0.14);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      padding: 12px;
      font: 12px/1.45 var(--vscode-font-family);
      color: var(--vscode-foreground);
      background:
        radial-gradient(circle at top right, color-mix(in srgb, var(--accent) 12%, transparent) 0, transparent 38%),
        linear-gradient(180deg, color-mix(in srgb, var(--vscode-sideBar-background) 94%, black 6%), var(--vscode-sideBar-background));
    }

    button, textarea {
      font: inherit;
    }

    .stack {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    .card {
      border: 1px solid var(--border);
      border-radius: 14px;
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .card-body {
      padding: 12px;
    }

    .composer {
      border-top: 3px solid var(--accent);
    }

    .eyebrow {
      margin: 0 0 6px;
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }

    h1, h2, h3, p {
      margin: 0;
    }

    .title-row, .meta-row, .button-row {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .title-row {
      justify-content: space-between;
      margin-bottom: 8px;
    }

    .goal {
      font-size: 14px;
      font-weight: 700;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--panel-strong);
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }

    .badge.pending { color: var(--accent); }
    .badge.negotiating, .badge.annotating { color: var(--warning); }
    .badge.complete, .badge.approved { color: var(--success); }
    .badge.skipped, .badge.redirected, .badge.altered { color: var(--danger); }

    textarea {
      width: 100%;
      min-height: 92px;
      resize: vertical;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      color: inherit;
      background: color-mix(in srgb, var(--vscode-input-background) 88%, transparent 12%);
    }

    textarea:focus {
      outline: 1px solid var(--accent);
      outline-offset: 1px;
    }

    .hint {
      color: var(--muted);
      margin-top: 8px;
    }

    .busy {
      color: var(--warning);
      margin-top: 8px;
    }

    .button-row {
      margin-top: 10px;
    }

    button {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 6px 10px;
      background: var(--panel-strong);
      color: inherit;
      cursor: pointer;
    }

    button.primary {
      border-color: color-mix(in srgb, var(--accent) 60%, transparent 40%);
      background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 28%, transparent), color-mix(in srgb, var(--accent) 14%, transparent));
    }

    button:hover:enabled {
      border-color: var(--accent);
    }

    button:disabled {
      opacity: 0.6;
      cursor: default;
    }

    .task-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }

    .task {
      border-top: 1px solid var(--border);
      padding-top: 10px;
    }

    .task:first-child {
      border-top: 0;
      padding-top: 0;
    }

    .task-title {
      font-weight: 700;
      font-size: 13px;
    }

    .task-description {
      margin-top: 6px;
      color: var(--muted);
    }

    .location {
      color: var(--muted);
      font-size: 11px;
    }

    .annotation {
      margin-top: 10px;
      padding: 10px;
      border-radius: 12px;
      background: color-mix(in srgb, var(--panel-strong) 88%, transparent 12%);
      border: 1px solid var(--border);
    }

    .annotation-summary {
      font-weight: 600;
    }

    .annotation-detail {
      margin-top: 6px;
      color: var(--muted);
    }

    .empty {
      color: var(--muted);
    }

    .persona-row {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }

    .persona-chip {
      padding: 4px 9px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--panel-strong);
      color: var(--muted);
      cursor: pointer;
      font-size: 11px;
      letter-spacing: 0.02em;
      transition: border-color 0.1s, color 0.1s, background 0.1s;
    }

    .persona-chip:hover:not(:disabled) {
      border-color: var(--accent);
      color: var(--vscode-foreground);
    }

    .persona-chip.selected {
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 20%, transparent);
      color: var(--accent);
    }
  </style>
</head>
<body>
  <div id="app"></div>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const root = document.getElementById("app");
    const savedState = vscode.getState() || {};
    const PERSONAS = [
      { id: "architect",     icon: "Arch", title: "The Architect",        tagline: "Requirements, feasibility, risks, trade-offs, technical direction" },
      { id: "pattern_expert",icon: "Pat",  title: "Design Pattern Expert", tagline: "Framework fit, patterns, anti-patterns, structural policy" },
      { id: "debug_detective",icon: "Det", title: "Debug Detective",       tagline: "Hypothesis-driven root cause analysis" },
      { id: "yolo",          icon: "YOLO", title: "YOLO",                  tagline: "Ship fast, minimal code, no gold-plating" },
      { id: "socratic",      icon: "Soc",  title: "Socratic Coach",        tagline: "Guides with questions instead of answers" },
      { id: "stub_wireframer", icon: "Stub", title: "Stub/Wireframes",     tagline: "Compilable skeletons, TODO handoff, design gaps" },
    ];
    let state = {
      plan: null,
      backlogSummary: { nextTask: null, readyTasks: [], totalReady: 0 },
      busyMessage: null,
      checkingForSession: true,
      debugActive: false,
      debugLocation: "",
      draftGoal: typeof savedState.draftGoal === "string" ? savedState.draftGoal : "",
      draftDebugIntent: typeof savedState.draftDebugIntent === "string" ? savedState.draftDebugIntent : "",
      draftDebugReason: typeof savedState.draftDebugReason === "string" ? savedState.draftDebugReason : "bug investigation",
      selectedPersona: typeof savedState.selectedPersona === "string" ? savedState.selectedPersona : "architect",
    };

    function escapeHtml(value) {
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function persistState() {
      vscode.setState({ draftGoal: state.draftGoal, draftDebugIntent: state.draftDebugIntent, draftDebugReason: state.draftDebugReason, selectedPersona: state.selectedPersona });
    }

    function formatCoord(coord) {
      const parts = [];
      if (coord && coord.file) {
        const fileParts = String(coord.file).split(/[\\\\/]/);
        parts.push(fileParts[fileParts.length - 1]);
      }
      if (coord && coord.method) {
        parts.push("::" + coord.method);
      } else if (coord && coord.class) {
        parts.push("::" + coord.class);
      }
      if (coord && typeof coord.line === "number") {
        parts.push(":" + coord.line);
      }
      return parts.join("");
    }

    function statusBadge(status) {
      return '<span class="badge ' + escapeHtml(status) + '">' + escapeHtml(status) + "</span>";
    }

    function renderAnnotation(task, annotation, disabled) {
      const detail = annotation.detail
        ? '<p class="annotation-detail">' + escapeHtml(annotation.detail) + "</p>"
        : "";
      const pendingActions = annotation.status === "pending"
        ? [
            button("Approve", "approveAnnotation", {
              annotationId: annotation.id,
            }, true, disabled),
            button("Alter", "alterAnnotation", {
              taskId: task.id,
              annotationId: annotation.id,
            }, false, disabled),
            button("Redirect", "redirectTask", {
              taskId: task.id,
            }, false, disabled),
          ].join("")
        : "";

      return [
        '<div class="annotation">',
        '<div class="title-row">',
        '<div class="annotation-summary">' + escapeHtml(annotation.summary) + "</div>",
        statusBadge(annotation.status),
        "</div>",
        detail,
        '<div class="button-row">',
        button("Review", "showAnnotation", {
          taskId: task.id,
          annotationId: annotation.id,
        }, false, disabled),
        pendingActions,
        "</div>",
        "</div>",
      ].join("");
    }

    function button(label, action, data, primary, disabled) {
      const attrs = Object.entries(data || {})
        .map(([key, value]) => 'data-' + key.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase()) + '="' + escapeHtml(value) + '"')
        .join(" ");
      return '<button type="button" class="' + (primary ? "primary" : "") + '" data-action="' + action + '" ' + attrs + (disabled ? " disabled" : "") + ">" + escapeHtml(label) + "</button>";
    }

    function renderPlan(plan) {
      if (!plan) {
        const status = state.checkingForSession
          ? '<p class="hint">Checking for an existing session...</p>'
          : '<p class="empty">No active session yet. Describe the next change above and start a session.</p>';
        return [
          '<section class="card">',
          '<div class="card-body">',
          '<p class="eyebrow">Current Session</p>',
          status,
          "</div>",
          "</section>",
        ].join("");
      }

      const tasks = (plan.tasks || []).map((task) => {
        const annotations = (task.annotations || []).map((annotation) =>
          renderAnnotation(task, annotation, Boolean(state.busyMessage)),
        ).join("");

        return [
          '<div class="task">',
          '<div class="title-row">',
          '<div>',
          '<div class="task-title">[' + escapeHtml(task.priority) + "] " + escapeHtml(task.title) + "</div>",
          '<div class="location">' + escapeHtml(formatCoord(task.targetCoord || {})) + "</div>",
          "</div>",
          statusBadge(task.status),
          "</div>",
          '<p class="task-description">' + escapeHtml(task.description || task.title) + "</p>",
          task.blockedReason ? '<p class="busy">Blocked: ' + escapeHtml(task.blockedReason) + "</p>" : "",
          '<div class="button-row">',
          button("Open", "openTask", { taskId: task.id }, false, Boolean(state.busyMessage)),
          task.annotations && task.annotations.length === 0
            ? button("Generate Intent", "generateAnnotation", { taskId: task.id }, false, Boolean(state.busyMessage))
            : "",
          task.status !== "complete" && task.status !== "skipped"
            ? button("Skip", "skipTask", { taskId: task.id }, false, Boolean(state.busyMessage))
            : "",
          "</div>",
          annotations,
          "</div>",
        ].join("");
      }).join("");

      return [
        '<section class="card">',
        '<div class="card-body">',
        '<div class="title-row">',
        '<div>',
        '<p class="eyebrow">Current Session</p>',
        '<h2 class="goal">' + escapeHtml(plan.goalStatement) + "</h2>",
        "</div>",
        statusBadge(plan.status || "active"),
        "</div>",
        '<div class="task-list">' + (tasks || '<p class="empty">Planning has not produced tasks yet.</p>') + "</div>",
        "</div>",
        "</section>",
      ].join("");
    }

    function renderBacklog(backlogSummary) {
      if (!state.plan || !backlogSummary || backlogSummary.totalReady <= 0) {
        return "";
      }

      const nextTask = backlogSummary.nextTask;
      const remaining = (backlogSummary.readyTasks || []).filter((task) =>
        !nextTask || task.id !== nextTask.id,
      );
      const nextMarkup = nextTask
        ? [
            '<div class="annotation" style="margin-top:0">',
            '<p class="eyebrow">What Next</p>',
            '<div class="title-row">',
            '<div class="annotation-summary">[' + escapeHtml(nextTask.priority) + "] " + escapeHtml(nextTask.title) + "</div>",
            statusBadge(nextTask.status || "pending"),
            "</div>",
            '<p class="location">' + escapeHtml(formatCoord(nextTask.targetCoord || {})) + "</p>",
            nextTask.blockedReason ? '<p class="busy">Blocked: ' + escapeHtml(nextTask.blockedReason) + "</p>" : "",
            "</div>",
          ].join("")
        : '<p class="empty">No ready backlog task.</p>';

      const restMarkup = remaining.length > 0
        ? remaining.map((task) => [
            '<div class="task">',
            '<div class="title-row">',
            '<div class="task-title">[' + escapeHtml(task.priority) + "] " + escapeHtml(task.title) + "</div>",
            statusBadge(task.status || "pending"),
            "</div>",
            '<p class="location">' + escapeHtml(formatCoord(task.targetCoord || {})) + "</p>",
            task.phase ? '<p class="task-description">Phase: ' + escapeHtml(task.phase) + "</p>" : "",
            "</div>",
          ].join("")).join("")
        : '<p class="empty">No additional ready backlog tasks.</p>';

      return [
        '<section class="card">',
        '<div class="card-body">',
        '<div class="title-row">',
        '<div>',
        '<p class="eyebrow">Backlog Handoff</p>',
        '<h3>' + escapeHtml(String(backlogSummary.totalReady)) + " ready task(s)</h3>",
        "</div>",
        "</div>",
        nextMarkup,
        '<div class="task-list" style="margin-top:10px">' + restMarkup + "</div>",
        "</div>",
        "</section>",
      ].join("");
    }

    function renderDebugPanel() {
      if (!state.debugActive) {
        return "";
      }
      const loc = state.debugLocation ? '<p class="location" style="margin-bottom:8px">' + escapeHtml(state.debugLocation) + "</p>" : "";
      const stopReasons = ["bug investigation", "exception", "understanding flow", "data inspection", "other"];
      const options = stopReasons.map((r) =>
        '<option value="' + escapeHtml(r) + '"' + (state.draftDebugReason === r ? " selected" : "") + ">" + escapeHtml(r) + "</option>"
      ).join("");
      return [
        '<section class="card" style="border-top:3px solid var(--warning)">',
        '<div class="card-body">',
        '<p class="eyebrow">Debug Investigation</p>',
        loc,
        '<label style="display:block;margin-bottom:4px;color:var(--muted);font-size:11px">What do you want to investigate?</label>',
        '<textarea id="debug-intent-input" placeholder="e.g. Why is user.balance going negative?" style="min-height:64px">' + escapeHtml(state.draftDebugIntent) + "</textarea>",
        '<label style="display:block;margin-top:8px;margin-bottom:4px;color:var(--muted);font-size:11px">Why did you stop here?</label>',
        '<select id="debug-reason-select" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:10px;background:var(--panel-strong);color:inherit;font:inherit">',
        options,
        "</select>",
        '<div class="button-row">',
        '<button type="button" class="primary" data-action="pushDebugToAgent">Push to Agent</button>',
        "</div>",
        '<p class="hint">Snapshot written to <code>.waterfree/debug/snapshot.json</code> — agent queries it via <code>mcp_debug</code> tools.</p>',
        "</div>",
        "</section>",
      ].join("");
    }

    function render() {
      const disabled = Boolean(state.busyMessage);
      root.innerHTML = [
        '<div class="stack">',
        '<section class="card composer">',
        '<div class="card-body">',
        '<div class="title-row">',
        '<div>',
        '<p class="eyebrow">New Session</p>',
        "<h1>Create a prompt</h1>",
        "</div>",
        disabled && state.busyMessage ? '<span class="badge pending">' + escapeHtml(state.busyMessage) + "</span>" : "",
        "</div>",
        '<textarea id="goal-input" placeholder="Describe what to build or fix.">' + escapeHtml(state.draftGoal) + "</textarea>",
        '<label style="display:block;margin-top:8px;margin-bottom:4px;color:var(--muted);font-size:11px">Personality</label>',
        '<div class="persona-row">',
        PERSONAS.map((p) => {
          const sel = state.selectedPersona === p.id ? " selected" : "";
          const dis = disabled ? " disabled" : "";
          return '<button type="button" class="persona-chip' + sel + '" data-action="selectPersona" data-persona-id="' + escapeHtml(p.id) + '" title="' + escapeHtml(p.title + ': ' + p.tagline) + '"' + dis + '>' + escapeHtml(p.icon) + '</button>';
        }).join(""),
        '</div>',
        '<div class="button-row">',
        '<button type="button" class="primary" data-action="startSession" ' + (disabled ? "disabled" : "") + ">Start Session</button>",
        '<button type="button" data-action="buildKnowledge" title="Pair Snippetize: extract reusable patterns from this workspace into the global snippet store" ' + (disabled ? "disabled" : "") + ">&#x1F9E0; Snippetize</button>",
        '<button type="button" data-action="addKnowledgeRepo" title="Pair Snippetize: snippetize an external git repo or local path" ' + (disabled ? "disabled" : "") + ">+ Snippetize Repo</button>",
        "</div>",
        '<p class="hint">The sidebar stays ready for the next prompt even when a session is active.</p>',
        state.busyMessage ? '<p class="busy">' + escapeHtml(state.busyMessage) + "</p>" : "",
        "</div>",
        "</section>",
        renderDebugPanel(),
        renderPlan(state.plan),
        renderBacklog(state.backlogSummary),
        "</div>",
      ].join("");

      const input = document.getElementById("goal-input");
      if (input) {
        input.addEventListener("input", (event) => {
          state.draftGoal = event.target.value;
          persistState();
        });
      }
      const debugIntentInput = document.getElementById("debug-intent-input");
      if (debugIntentInput) {
        debugIntentInput.addEventListener("input", (event) => {
          state.draftDebugIntent = event.target.value;
          persistState();
        });
      }
      const debugReasonSelect = document.getElementById("debug-reason-select");
      if (debugReasonSelect) {
        debugReasonSelect.addEventListener("change", (event) => {
          state.draftDebugReason = event.target.value;
          persistState();
        });
      }
    }

    root.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) {
        return;
      }
      const action = button.getAttribute("data-action");
      if (!action) {
        return;
      }

      if (action === "selectPersona") {
        const personaId = button.getAttribute("data-persona-id");
        if (personaId && !state.busyMessage) {
          state.selectedPersona = personaId;
          persistState();
          render();
        }
        return;
      }

      if (action === "startSession") {
        const goal = state.draftGoal.trim();
        if (goal && !state.busyMessage) {
          vscode.postMessage({ type: "startSession", goal, persona: state.selectedPersona });
        }
        return;
      }

      if (action === "buildKnowledge") {
        if (!state.busyMessage) {
          const goal = state.draftGoal.trim();
          const match = goal.match(/^Extract and explain (.+?) for snippetize\\.\\s*Add context:\\s*(.*)$/s);
          if (match) {
            vscode.postMessage({ type: "snippetizeSymbol", symbol: match[1].trim(), context: match[2].trim() });
          } else {
            vscode.postMessage({ type: "buildKnowledge" });
          }
        }
        return;
      }

      if (action === "addKnowledgeRepo") {
        if (!state.busyMessage) {
          vscode.postMessage({ type: "addKnowledgeRepo" });
        }
        return;
      }

      if (action === "pushDebugToAgent") {
        if (!state.busyMessage) {
          vscode.postMessage({ type: "pushDebugToAgent", intent: state.draftDebugIntent, stopReason: state.draftDebugReason });
        }
        return;
      }

      if (state.busyMessage) {
        return;
      }

      const taskId = button.getAttribute("data-task-id") || undefined;
      const annotationId = button.getAttribute("data-annotation-id") || undefined;

      switch (action) {
        case "openTask":
        case "generateAnnotation":
        case "skipTask":
          if (taskId) {
            vscode.postMessage({ type: action, taskId });
          }
          return;
        case "showAnnotation":
          if (taskId) {
            vscode.postMessage({ type: "showAnnotation", taskId, annotationId });
          }
          return;
        case "approveAnnotation":
          if (annotationId) {
            vscode.postMessage({ type: "approveAnnotation", annotationId });
          }
          return;
        case "alterAnnotation":
          if (taskId && annotationId) {
            const feedback = window.prompt("What should be different?");
            if (feedback && feedback.trim()) {
              vscode.postMessage({
                type: "alterAnnotation",
                taskId,
                annotationId,
                feedback,
              });
            }
          }
          return;
        case "redirectTask":
          if (taskId) {
            const instruction = window.prompt("Give a new direction for this task:");
            if (instruction && instruction.trim()) {
              vscode.postMessage({
                type: "redirectTask",
                taskId,
                instruction,
              });
            }
          }
          return;
      }
    });

    window.addEventListener("message", (event) => {
      const message = event.data || {};
      if (message.type === "state") {
        state = {
          ...state,
          ...message.state,
        };
        render();
      } else if (message.type === "clearComposer") {
        state.draftGoal = "";
        persistState();
        render();
      } else if (message.type === "prefillSnippetize") {
        const sym = message.symbol || "";
        state.draftGoal = "Extract and explain " + sym + " for snippetize. Add context: ";
        persistState();
        render();
        const input = document.getElementById("goal-input");
        if (input) {
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
        }
      }
    });

    render();
  </script>
</body>
</html>`;
  }
}

function clampToZeroBased(line?: number): number {
  if (!line || line < 1) {
    return 0;
  }
  return line - 1;
}

function formatCoord(coord: CodeCoord): string {
  const file = coord.file ? path.basename(coord.file) : "";
  const symbol = coord.method ?? coord.class ?? "";
  const symbolLabel = symbol ? `${file}::${symbol}` : file;
  return coord.line ? `${symbolLabel}:${coord.line}` : symbolLabel;
}

function getNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let value = "";
  for (let i = 0; i < 32; i += 1) {
    value += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
