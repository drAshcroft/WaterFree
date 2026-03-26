import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

export type TutorializePanelAction =
  | { type: "send"; sessionId: string; message: string }
  | { type: "cancel"; sessionId: string };

export type TutorializeMessage = {
  role: "user" | "assistant" | "progress";
  text: string;
};

export class TutorializePanel implements vscode.Disposable {
  private readonly _actionEmitter = new vscode.EventEmitter<TutorializePanelAction>();
  private readonly _disposables: vscode.Disposable[] = [];
  private readonly _extensionUri: vscode.Uri;
  private _panel: vscode.WebviewPanel | null = null;

  readonly onDidTriggerAction = this._actionEmitter.event;

  constructor(extensionUri: vscode.Uri) {
    this._extensionUri = extensionUri;
  }

  show(focus: string, sessionId: string, repoPath?: string): void {
    const panel = this._ensurePanel();
    panel.reveal(vscode.ViewColumn.Active, false);
    void panel.webview.postMessage({ type: "init", focus, sessionId, repoPath: repoPath ?? "" });
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
      "waterfreeTutorialize",
      "Tutorialize",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.joinPath(this._extensionUri, "src", "ui", "tutorialize-panel"),
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
      case "send":
        if (typeof message.sessionId === "string" && typeof message.message === "string") {
          this._actionEmitter.fire({ type: "send", sessionId: message.sessionId, message: message.message });
        }
        return;
      case "cancel":
        if (typeof message.sessionId === "string") {
          this._actionEmitter.fire({ type: "cancel", sessionId: message.sessionId });
        }
        return;
    }
  }

  private _getHtml(webview: vscode.Webview): string {
    const nonce = getNonce();
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "tutorialize-panel", "tutorialize-panel.css"),
    );
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "tutorialize-panel", "tutorialize-panel.js"),
    );
    const htmlPath = vscode.Uri.joinPath(
      this._extensionUri,
      "src",
      "ui",
      "tutorialize-panel",
      "tutorialize-panel.html",
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
