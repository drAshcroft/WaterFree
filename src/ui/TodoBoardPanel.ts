import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

type TodoPriority = "P0" | "P1" | "P2" | "P3" | "spike";
type TodoStatus = "pending" | "annotating" | "negotiating" | "executing" | "complete" | "skipped";
type TodoOwnerType = "human" | "agent" | "unassigned";
type TodoTaskType = "impl" | "test" | "spike" | "review" | "refactor" | "protocol" | "bug_fix" | "feature" | "task";
type TodoTiming = "one_time" | "recurring";
type TodoDependencyType = "blocks" | "informs" | "shares-file";
type TodoAnchorType = "create-at" | "modify" | "delete" | "read-only-context";

export interface TodoBoardCoord {
  file: string;
  class?: string | null;
  method?: string | null;
  line?: number | null;
  anchorType: TodoAnchorType;
}

export interface TodoBoardDependency {
  taskId: string;
  type: TodoDependencyType;
}

export interface TodoBoardOwner {
  type: TodoOwnerType;
  name: string;
  assignedAt?: string | null;
}

export interface TodoBoardTaskData {
  id: string;
  title: string;
  description: string;
  rationale?: string | null;
  targetCoord: TodoBoardCoord;
  contextCoords: TodoBoardCoord[];
  priority: TodoPriority;
  phase?: string | null;
  dependsOn: TodoBoardDependency[];
  blockedReason?: string | null;
  owner: TodoBoardOwner;
  taskType: TodoTaskType;
  estimatedMinutes?: number | null;
  actualMinutes?: number | null;
  status: TodoStatus;
  humanNotes?: string | null;
  aiNotes?: string | null;
  annotations: unknown[];
  startedAt?: string | null;
  completedAt?: string | null;
  acceptanceCriteria?: string | null;
  trigger?: string | null;
  timing: TodoTiming;
}

export interface TodoBoardState {
  tasks: TodoBoardTaskData[];
  phases: string[];
  updatedAt?: string;
  path?: string;
}

export type TodoBoardAction =
  | { type: "refresh" }
  | { type: "openTask"; file: string; line?: number }
  | { type: "addTask"; task: Record<string, unknown> }
  | { type: "updateTask"; taskId: string; patch: Record<string, unknown> }
  | { type: "deleteTask"; taskId: string }
  | { type: "saveBoard"; tasks: Array<{ id: string; phase?: string | null }>; phases: string[] };

export class TodoBoardPanel implements vscode.Disposable {
  private readonly _actionEmitter = new vscode.EventEmitter<TodoBoardAction>();
  private readonly _disposables: vscode.Disposable[] = [];
  private readonly _extensionUri: vscode.Uri;
  private _panel: vscode.WebviewPanel | null = null;
  private _state: TodoBoardState | null = null;

  readonly onDidTriggerAction = this._actionEmitter.event;

  constructor(extensionUri: vscode.Uri) {
    this._extensionUri = extensionUri;
  }

  async show(state: TodoBoardState): Promise<void> {
    this._state = state;
    const panel = this._ensurePanel();
    panel.title = "WaterFree Todo Tree";
    await panel.webview.postMessage({ type: "state", state });
    panel.reveal(vscode.ViewColumn.Active, false);
  }

  async update(state: TodoBoardState): Promise<void> {
    this._state = state;
    if (!this._panel) {
      return;
    }
    await this._panel.webview.postMessage({ type: "state", state });
  }

  isVisible(): boolean {
    return this._panel !== null;
  }

  dispose(): void {
    this._panel?.dispose();
    for (const disposable of this._disposables) {
      disposable.dispose();
    }
    this._disposables.length = 0;
    this._actionEmitter.dispose();
  }

  private _ensurePanel(): vscode.WebviewPanel {
    if (this._panel) {
      return this._panel;
    }

    const panel = vscode.window.createWebviewPanel(
      "waterfreeTodoBoard",
      "WaterFree Todo Tree",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.joinPath(this._extensionUri, "src", "ui", "todo-board"),
        ],
      },
    );

    panel.webview.html = this._getHtml(panel.webview);

    this._disposables.push(
      panel.webview.onDidReceiveMessage((message: unknown) => {
        this._handleMessage(message);
      }),
      panel.onDidDispose(() => {
        this._panel = null;
      }),
    );

    this._panel = panel;
    if (this._state) {
      void panel.webview.postMessage({ type: "state", state: this._state });
    }
    return panel;
  }

  private _handleMessage(message: unknown): void {
    if (!isRecord(message) || typeof message.type !== "string") {
      return;
    }

    switch (message.type) {
      case "refresh":
        this._actionEmitter.fire({ type: "refresh" });
        return;
      case "openTask":
        if (typeof message.file === "string" && message.file.trim()) {
          this._actionEmitter.fire({
            type: "openTask",
            file: message.file.trim(),
            line: typeof message.line === "number" ? message.line : undefined,
          });
        }
        return;
      case "addTask":
        if (isRecord(message.task)) {
          this._actionEmitter.fire({ type: "addTask", task: message.task });
        }
        return;
      case "updateTask":
        if (typeof message.taskId === "string" && isRecord(message.patch)) {
          this._actionEmitter.fire({
            type: "updateTask",
            taskId: message.taskId,
            patch: message.patch,
          });
        }
        return;
      case "deleteTask":
        if (typeof message.taskId === "string") {
          this._actionEmitter.fire({ type: "deleteTask", taskId: message.taskId });
        }
        return;
      case "saveBoard":
        if (Array.isArray(message.tasks) && Array.isArray(message.phases)) {
          const tasks = message.tasks
            .filter(isRecord)
            .map((item) => ({
              id: typeof item.id === "string" ? item.id : "",
              phase: typeof item.phase === "string" ? item.phase : null,
            }))
            .filter((item) => item.id);
          const phases = message.phases.map((phase) => String(phase));
          this._actionEmitter.fire({ type: "saveBoard", tasks, phases });
        }
        return;
    }
  }

  private _getHtml(webview: vscode.Webview): string {
    const nonce = getNonce();
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "todo-board", "todo-board.css"),
    );
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "todo-board", "todo-board.js"),
    );
    const htmlPath = vscode.Uri.joinPath(
      this._extensionUri,
      "src",
      "ui",
      "todo-board",
      "todo-board.html",
    );
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}' ${webview.cspSource}`,
    ].join("; ");

    let html = fs.readFileSync(path.normalize(htmlPath.fsPath), "utf8");
    html = html
      .replace("{{CSP}}", csp)
      .replace("{{STYLE_URI}}", styleUri.toString())
      .replace("{{SCRIPT_URI}}", scriptUri.toString())
      .replace("{{NONCE}}", nonce);
    return html;
  }
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
