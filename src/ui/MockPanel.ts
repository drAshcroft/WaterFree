/**
 * MockPanel — VS Code webview panel for the WaterFree interactive Mock runtime.
 *
 * When MockRuntime runs in interactive mode it writes capture_{id}.json to
 *   {workspaceRoot}/.waterfree/mock/
 * and blocks waiting for a matching response_{id}.json.
 *
 * This panel watches that directory, shows the system prompt and user prompt
 * in a read-only view, and writes the response file when the user submits.
 *
 * Usage:
 *   const panel = new MockPanel(extensionUri, workspacePath);
 *   panel.attach(context);
 *   context.subscriptions.push(panel);
 */

import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

type CaptureRecord = {
  id: string;
  stage: string;
  persona: string;
  system: string;
  user: string;
  timestamp: string;
  status: string;
};

export class MockPanel implements vscode.Disposable {
  private readonly _extensionUri: vscode.Uri;
  private readonly _captureDir: string;
  private readonly _disposables: vscode.Disposable[] = [];

  private _panel: vscode.WebviewPanel | null = null;
  private _queue: CaptureRecord[] = [];
  private _active: CaptureRecord | null = null;

  constructor(extensionUri: vscode.Uri, workspacePath: string) {
    this._extensionUri = extensionUri;
    this._captureDir = path.join(workspacePath, ".waterfree", "mock");
  }

  // ------------------------------------------------------------------
  // Public API
  // ------------------------------------------------------------------

  attach(context: vscode.ExtensionContext): void {
    const pattern = new vscode.RelativePattern(this._captureDir, "capture_*.json");
    const watcher = vscode.workspace.createFileSystemWatcher(pattern, false, true, true);
    this._disposables.push(
      watcher,
      watcher.onDidCreate((uri) => this._onCaptureCreated(uri)),
    );

    this._disposables.push(
      vscode.commands.registerCommand("waterfree.openMockPanel", () => {
        this._ensurePanel();
        this._panel?.reveal(vscode.ViewColumn.Two, false);
      }),
    );

    context.subscriptions.push(...this._disposables);
    this._scanExistingCaptures();
  }

  dispose(): void {
    this._panel?.dispose();
    for (const d of this._disposables) d.dispose();
    this._disposables.length = 0;
  }

  // ------------------------------------------------------------------
  // Filesystem events
  // ------------------------------------------------------------------

  private _onCaptureCreated(uri: vscode.Uri): void {
    const capture = this._readCapture(uri.fsPath);
    if (!capture) return;
    if (this._active === null) {
      this._showCapture(capture);
    } else {
      this._queue.push(capture);
      this._postQueueSize();
    }
  }

  private _scanExistingCaptures(): void {
    if (!fs.existsSync(this._captureDir)) return;
    const files = fs
      .readdirSync(this._captureDir)
      .filter((f) => f.startsWith("capture_") && f.endsWith(".json"))
      .sort();
    for (const file of files) {
      const capture = this._readCapture(path.join(this._captureDir, file));
      if (!capture || capture.status !== "pending") continue;
      if (fs.existsSync(path.join(this._captureDir, `response_${capture.id}.json`))) continue;
      if (this._active === null) {
        this._showCapture(capture);
      } else {
        this._queue.push(capture);
      }
    }
    this._postQueueSize();
  }

  // ------------------------------------------------------------------
  // Panel management
  // ------------------------------------------------------------------

  private _showCapture(capture: CaptureRecord): void {
    this._active = capture;
    const panel = this._ensurePanel();
    panel.title = `Mock: ${capture.stage}`;
    void panel.webview.postMessage({ type: "capture", capture });
    panel.reveal(vscode.ViewColumn.Two, false);
    this._postQueueSize();
  }

  private _ensurePanel(): vscode.WebviewPanel {
    if (this._panel) return this._panel;
    const panel = vscode.window.createWebviewPanel(
      "waterFreeMock",
      "WaterFree Mock",
      vscode.ViewColumn.Two,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.joinPath(this._extensionUri, "src", "ui", "mock-panel"),
        ],
      },
    );
    panel.webview.html = this._getHtml(panel.webview);
    this._disposables.push(
      panel.webview.onDidReceiveMessage((msg: unknown) => this._handleMessage(msg)),
      panel.onDidDispose(() => { this._panel = null; }),
    );
    this._panel = panel;
    return panel;
  }

  // ------------------------------------------------------------------
  // Webview message handling
  // ------------------------------------------------------------------

  private _handleMessage(message: unknown): void {
    if (!isRecord(message) || typeof message.type !== "string") return;
    if (message.type === "submit" && typeof message.captureId === "string") {
      const response = typeof message.response === "string" ? message.response : "";
      this._writeResponse(message.captureId, response);
      this._advanceQueue();
    } else if (message.type === "discard" && typeof message.captureId === "string") {
      this._writeResponse(message.captureId, "");
      this._advanceQueue();
    }
  }

  private _writeResponse(captureId: string, response: string): void {
    const responsePath = path.join(this._captureDir, `response_${captureId}.json`);
    try {
      fs.mkdirSync(this._captureDir, { recursive: true });
      fs.writeFileSync(responsePath, JSON.stringify({ response }, null, 2), "utf8");
    } catch (err) {
      void vscode.window.showErrorMessage(`MockPanel: could not write response — ${String(err)}`);
    }
  }

  private _advanceQueue(): void {
    this._active = null;
    const next = this._queue.shift();
    if (next) {
      this._showCapture(next);
    } else {
      this._panel?.webview.postMessage({ type: "cleared" });
      this._panel?.webview.postMessage({ type: "queueSize", count: 0 });
    }
  }

  private _postQueueSize(): void {
    this._panel?.webview.postMessage({ type: "queueSize", count: this._queue.length });
  }

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------

  private _readCapture(filePath: string): CaptureRecord | null {
    try {
      const raw = fs.readFileSync(filePath, "utf8");
      const data = JSON.parse(raw) as Record<string, unknown>;
      return {
        id: String(data["id"] ?? ""),
        stage: String(data["stage"] ?? ""),
        persona: String(data["persona"] ?? "mock"),
        system: String(data["system"] ?? ""),
        user: String(data["user"] ?? ""),
        timestamp: String(data["timestamp"] ?? ""),
        status: String(data["status"] ?? "pending"),
      };
    } catch {
      return null;
    }
  }

  private _getHtml(webview: vscode.Webview): string {
    const nonce = getNonce();
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "mock-panel", "mock-panel.css"),
    );
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "mock-panel", "mock-panel.js"),
    );
    const htmlPath = vscode.Uri.joinPath(
      this._extensionUri, "src", "ui", "mock-panel", "mock-panel.html",
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
  let v = "";
  for (let i = 0; i < 32; i++) v += chars[Math.floor(Math.random() * chars.length)];
  return v;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
