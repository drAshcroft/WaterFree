import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

export interface IndexDashboardStatus {
  status?: string;
  project?: string;
  root_path?: string;
  indexed_at?: string;
  node_count?: number;
  edge_count?: number;
  db_path?: string;
}

export interface IndexDashboardSchema {
  project?: string;
  root_path?: string;
  db_path?: string;
  node_count?: number;
  edge_count?: number;
  node_labels?: Array<{ label: string; count: number }>;
  edge_types?: Array<{ type: string; count: number }>;
  relationship_patterns?: Array<{
    source_label: string;
    edge_type: string;
    target_label: string;
    count: number;
  }>;
}

export interface IndexDashboardState {
  workspacePath: string;
  updatedAt: string;
  status: IndexDashboardStatus;
  schema: IndexDashboardSchema;
  architecture: Record<string, unknown>;
}

export type IndexDashboardAction =
  | { type: "refresh" }
  | { type: "reindex" };

export class IndexDashboardPanel implements vscode.Disposable {
  private readonly _actionEmitter = new vscode.EventEmitter<IndexDashboardAction>();
  private readonly _disposables: vscode.Disposable[] = [];
  private readonly _extensionUri: vscode.Uri;
  private _panel: vscode.WebviewPanel | null = null;
  private _state: IndexDashboardState | null = null;

  readonly onDidTriggerAction = this._actionEmitter.event;

  constructor(extensionUri: vscode.Uri) {
    this._extensionUri = extensionUri;
  }

  async show(state: IndexDashboardState): Promise<void> {
    this._state = state;
    const panel = this._ensurePanel();
    panel.title = "WaterFree Index";
    await panel.webview.postMessage({ type: "state", state });
    panel.reveal(vscode.ViewColumn.Active, false);
  }

  async update(state: IndexDashboardState): Promise<void> {
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
      "waterfreeIndexDashboard",
      "WaterFree Index",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.joinPath(this._extensionUri, "src", "ui", "index-dashboard"),
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
      case "reindex":
        this._actionEmitter.fire({ type: "reindex" });
        return;
    }
  }

  private _getHtml(webview: vscode.Webview): string {
    const nonce = getNonce();
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "index-dashboard", "index-dashboard.css"),
    );
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "index-dashboard", "index-dashboard.js"),
    );
    const htmlPath = vscode.Uri.joinPath(
      this._extensionUri,
      "src",
      "ui",
      "index-dashboard",
      "index-dashboard.html",
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
