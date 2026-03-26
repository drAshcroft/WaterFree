/**
 * PlanSidebar — VS Code webview sidebar showing the current session plus a
 * persistent prompt composer for starting a new session.
 */

import * as fs from "fs";
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

export interface SessionRuntimeSelection {
  providerId?: string;
  model?: string;
}

export interface PersonaProviderAssignment {
  providerId: string;
  model: string;
  stages: Array<
    | "planning"
    | "annotation"
    | "execution"
    | "debug"
    | "question_answer"
    | "ripple_detection"
    | "alter_annotation"
    | "knowledge"
  >;
}

function isProviderStage(value: string): value is PersonaProviderAssignment["stages"][number] {
  return [
    "planning",
    "annotation",
    "execution",
    "debug",
    "question_answer",
    "ripple_detection",
    "alter_annotation",
    "knowledge",
  ].includes(value);
}

export type SidebarAction =
  | { type: "startSession"; goal: string; persona: string; runtimeSelection?: SessionRuntimeSelection }
  | { type: "startTutorialize"; goal: string; runtimeSelection?: SessionRuntimeSelection }
  | { type: "openWizard"; wizardId: string; goal: string; persona: string }
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
  | { type: "pushDebugToAgent"; intent: string; stopReason: string }
  | { type: "openTodoBoard" }
  | { type: "openKnowledge" }
  | { type: "openPersonaStudio" }
  | { type: "requestHistory" }
  | { type: "restoreSession"; file: string }
  | { type: "requestSettings" }
  | { type: "addProvider"; providerType: string; name: string; apiKey: string; baseUrl: string; models: string[]; enabled: boolean }
  | { type: "updateProvider"; id: string; providerType: string; name: string; apiKey: string; baseUrl: string; models: string[]; enabled: boolean }
  | { type: "savePersonaAssignments"; personaId: string; assignments: PersonaProviderAssignment[] }
  | { type: "removeProvider"; id: string }
  | { type: "toggleProvider"; id: string }
  | { type: "requestUsageStats" };

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
  private readonly _extensionUri: vscode.Uri;

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

  constructor(extensionUri: vscode.Uri) {
    this._extensionUri = extensionUri;
  }

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this._view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this._extensionUri, "src", "ui", "sidebar"),
      ],
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

  openSettings(): void {
    void this._view?.webview.postMessage({ type: "openSettings" });
  }

  clearComposer(): void {
    void this._view?.webview.postMessage({ type: "clearComposer" });
  }

  prefillSnippetize(symbol: string): void {
    void this._view?.webview.postMessage({ type: "prefillSnippetize", symbol });
  }

  sendSettings(data: unknown): void {
    void this._view?.webview.postMessage({ type: "settings", data });
  }

  sendUsageStats(data: unknown): void {
    void this._view?.webview.postMessage({ type: "usageStats", data });
  }

  sendHistory(sessions: unknown[]): void {
    void this._view?.webview.postMessage({ type: "history", sessions });
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
          const runtimeSelection = isRecord(message.runtimeSelection)
            ? {
                providerId: typeof message.runtimeSelection.providerId === "string"
                  ? message.runtimeSelection.providerId.trim()
                  : undefined,
                model: typeof message.runtimeSelection.model === "string"
                  ? message.runtimeSelection.model.trim()
                  : undefined,
              }
            : undefined;
          this._actionEmitter.fire({
            type: "startSession",
            goal: message.goal.trim(),
            persona: typeof message.persona === "string" ? message.persona : "architect",
            runtimeSelection: runtimeSelection && (runtimeSelection.providerId || runtimeSelection.model)
              ? runtimeSelection
              : undefined,
          });
        }
        return;
      case "openWizard":
        if (typeof message.wizardId === "string") {
          this._actionEmitter.fire({
            type: "openWizard",
            wizardId: message.wizardId,
            goal: typeof message.goal === "string" ? message.goal.trim() : "",
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
      case "openTodoBoard":
        this._actionEmitter.fire({ type: "openTodoBoard" });
        return;
      case "openKnowledge":
        this._actionEmitter.fire({ type: "openKnowledge" });
        return;
      case "openPersonaStudio":
        this._actionEmitter.fire({ type: "openPersonaStudio" });
        return;
      case "requestHistory":
        this._actionEmitter.fire({ type: "requestHistory" });
        return;
      case "restoreSession":
        if (typeof message.file === "string") {
          this._actionEmitter.fire({ type: "restoreSession", file: message.file });
        }
        return;
      case "requestSettings":
        this._actionEmitter.fire({ type: "requestSettings" });
        return;
      case "requestUsageStats":
        this._actionEmitter.fire({ type: "requestUsageStats" });
        return;
      case "addProvider":
        this._actionEmitter.fire({
          type: "addProvider",
          providerType: String(message.providerType ?? "claude"),
          name: String(message.name ?? ""),
          apiKey: String(message.apiKey ?? ""),
          baseUrl: String(message.baseUrl ?? ""),
          models: Array.isArray(message.models) ? message.models.map(String) : [],
          enabled: Boolean(message.enabled ?? true),
        });
        return;
      case "updateProvider":
        if (typeof message.id === "string") {
          this._actionEmitter.fire({
            type: "updateProvider",
            id: message.id,
            providerType: String(message.providerType ?? "claude"),
            name: String(message.name ?? ""),
            apiKey: String(message.apiKey ?? ""),
            baseUrl: String(message.baseUrl ?? ""),
            models: Array.isArray(message.models) ? message.models.map(String) : [],
            enabled: Boolean(message.enabled ?? true),
          });
        }
        return;
      case "savePersonaAssignments":
        if (typeof message.personaId === "string") {
          this._actionEmitter.fire({
            type: "savePersonaAssignments",
            personaId: message.personaId,
            assignments: Array.isArray(message.assignments)
              ? message.assignments
                .filter(isRecord)
                .map((entry) => ({
                  providerId: String(entry.providerId ?? ""),
                  model: String(entry.model ?? ""),
                  stages: Array.isArray(entry.stages)
                    ? entry.stages.map(String).filter(isProviderStage)
                    : [],
                }))
              : [],
          });
        }
        return;
      case "removeProvider":
        if (typeof message.id === "string") {
          this._actionEmitter.fire({ type: "removeProvider", id: message.id });
        }
        return;
      case "toggleProvider":
        if (typeof message.id === "string") {
          this._actionEmitter.fire({ type: "toggleProvider", id: message.id });
        }
        return;
    }
  }

  private _getHtml(webview: vscode.Webview): string {
    const nonce = getNonce();
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "sidebar", "sidebar.css"),
    );
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "sidebar", "sidebar.js"),
    );
    const htmlPath = vscode.Uri.joinPath(
      this._extensionUri, "src", "ui", "sidebar", "sidebar.html",
    );

    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}' ${webview.cspSource}`,
    ].join("; ");

    let html = fs.readFileSync(htmlPath.fsPath, "utf8");
    html = html
      .replace("{{CSP}}", csp)
      .replace("{{STYLE_URI}}", styleUri.toString())
      .replace("{{NONCE}}", nonce)
      .replace("{{SCRIPT_URI}}", scriptUri.toString());
    return html;
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
