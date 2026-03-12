import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

export type KnowledgePanelAction =
  | { type: "search"; query: string; limit: number }
  | { type: "browse"; path: string; depth: number; includeEntries: boolean; entryLimit: number }
  | { type: "loadSources" }
  | { type: "addEntry"; title: string; description: string; code: string; snippet_type: string; source_repo: string; source_file: string; tags: string[]; hierarchy_path: string; context: string }
  | { type: "deleteEntry"; id: string };

export class KnowledgePanel implements vscode.Disposable {
  private readonly _actionEmitter = new vscode.EventEmitter<KnowledgePanelAction>();
  private readonly _disposables: vscode.Disposable[] = [];
  private readonly _extensionUri: vscode.Uri;
  private _panel: vscode.WebviewPanel | null = null;

  readonly onDidTriggerAction = this._actionEmitter.event;

  constructor(extensionUri: vscode.Uri) {
    this._extensionUri = extensionUri;
  }

  show(): void {
    const panel = this._ensurePanel();
    panel.reveal(vscode.ViewColumn.Active, false);
  }

  isVisible(): boolean {
    return this._panel !== null;
  }

  async postMessage(msg: Record<string, unknown>): Promise<void> {
    await this._panel?.webview.postMessage(msg);
  }

  dispose(): void {
    this._panel?.dispose();
    for (const d of this._disposables) {
      d.dispose();
    }
    this._disposables.length = 0;
    this._actionEmitter.dispose();
  }

  private _ensurePanel(): vscode.WebviewPanel {
    if (this._panel) {
      return this._panel;
    }

    const panel = vscode.window.createWebviewPanel(
      "waterfreeKnowledge",
      "Knowledge Explorer",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.joinPath(this._extensionUri, "src", "ui", "knowledge-panel"),
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
    return panel;
  }

  private _handleMessage(message: unknown): void {
    if (!isRecord(message) || typeof message.type !== "string") {
      return;
    }

    switch (message.type) {
      case "search":
        this._actionEmitter.fire({
          type: "search",
          query: typeof message.query === "string" ? message.query : "",
          limit: typeof message.limit === "number" ? message.limit : 30,
        });
        return;
      case "browse":
        this._actionEmitter.fire({
          type: "browse",
          path: typeof message.path === "string" ? message.path : "",
          depth: typeof message.depth === "number" ? message.depth : 1,
          includeEntries: message.includeEntries === true,
          entryLimit: typeof message.entryLimit === "number" ? message.entryLimit : 50,
        });
        return;
      case "loadSources":
        this._actionEmitter.fire({ type: "loadSources" });
        return;
      case "addEntry":
        if (
          typeof message.title === "string" &&
          typeof message.description === "string" &&
          typeof message.code === "string" &&
          typeof message.source_repo === "string"
        ) {
          this._actionEmitter.fire({
            type: "addEntry",
            title: message.title,
            description: message.description,
            code: message.code,
            snippet_type: typeof message.snippet_type === "string" ? message.snippet_type : "pattern",
            source_repo: message.source_repo,
            source_file: typeof message.source_file === "string" ? message.source_file : "",
            tags: Array.isArray(message.tags) ? message.tags.map(String) : [],
            hierarchy_path: typeof message.hierarchy_path === "string" ? message.hierarchy_path : "",
            context: typeof message.context === "string" ? message.context : "",
          });
        }
        return;
      case "deleteEntry":
        if (typeof message.id === "string") {
          this._actionEmitter.fire({ type: "deleteEntry", id: message.id });
        }
        return;
    }
  }

  private _getHtml(webview: vscode.Webview): string {
    const nonce = getNonce();
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "knowledge-panel", "knowledge-panel.css"),
    );
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "knowledge-panel", "knowledge-panel.js"),
    );
    const htmlPath = vscode.Uri.joinPath(
      this._extensionUri,
      "src",
      "ui",
      "knowledge-panel",
      "knowledge-panel.html",
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
