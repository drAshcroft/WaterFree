import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

export interface PersonaStudioTool {
  name: string;
  title: string;
  category: string;
  readOnly: boolean;
  serverId: string;
}

export interface PersonaStudioSubagent {
  enabled: boolean;
  description: string;
  promptStage: string;
}

export interface PersonaStudioPersona {
  id: string;
  name: string;
  tagline: string;
  icon?: string;
  systemFragment: string;
  stageFragments?: Record<string, string>;
  tools?: PersonaStudioTool[];
  skillMarkdown: string;
  metadataJson: string;
  preferredModelTiers?: Record<string, string[]>;
  toolCategories?: string[];
  preferredSkillIds?: string[];
  subagent?: PersonaStudioSubagent;
}

export interface PersonaStudioDocument {
  personaId: string;
  skillMarkdown: string;
  metadataJson: string;
}

export interface PersonaStudioState {
  personas: PersonaStudioPersona[];
}

export type PersonaStudioAction =
  | { type: "save"; personas: PersonaStudioDocument[] };

export class PersonaStudioPanel implements vscode.Disposable {
  private readonly _actionEmitter = new vscode.EventEmitter<PersonaStudioAction>();
  private readonly _disposables: vscode.Disposable[] = [];
  private readonly _extensionUri: vscode.Uri;
  private _panel: vscode.WebviewPanel | null = null;
  private _state: PersonaStudioState | null = null;

  readonly onDidTriggerAction = this._actionEmitter.event;

  constructor(extensionUri: vscode.Uri) {
    this._extensionUri = extensionUri;
  }

  async show(state: PersonaStudioState): Promise<void> {
    this._state = state;
    const panel = this._ensurePanel();
    panel.title = "Persona Studio";
    await panel.webview.postMessage({ type: "state", state });
    panel.reveal(vscode.ViewColumn.Active, false);
  }

  async update(state: PersonaStudioState): Promise<void> {
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
      "waterfreePersonaStudio",
      "Persona Studio",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.joinPath(this._extensionUri, "src", "ui", "persona-studio"),
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
      case "save":
        if (Array.isArray(message.personas)) {
          const personas = message.personas
            .filter(isRecord)
            .map((item) => ({
              personaId: typeof item.personaId === "string" ? item.personaId : "",
              skillMarkdown: typeof item.skillMarkdown === "string" ? item.skillMarkdown : "",
              metadataJson: typeof item.metadataJson === "string" ? item.metadataJson : "",
            }))
            .filter((item) => item.personaId && item.skillMarkdown && item.metadataJson);
          this._actionEmitter.fire({ type: "save", personas });
        }
        return;
    }
  }

  private _getHtml(webview: vscode.Webview): string {
    const nonce = getNonce();
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "persona-studio", "persona-studio.css"),
    );
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "persona-studio", "persona-studio.js"),
    );
    const htmlPath = vscode.Uri.joinPath(
      this._extensionUri,
      "src",
      "ui",
      "persona-studio",
      "persona-studio.html",
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
