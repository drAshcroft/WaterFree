import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

import type {
  WizardChunkData,
  WizardResponse,
  WizardRunData,
  WizardStageData,
} from "../bridge/PythonBridge.js";
import type { WizardDocContext } from "../wizard/WizardDocState.js";

export type WizardEditorAction =
  | {
      type: "generate";
      context: WizardDocContext;
      body: string;
    }
  | {
      type: "refine";
      context: WizardDocContext;
      body: string;
    }
  | {
      type: "acceptChunk";
      context: WizardDocContext;
      chunkId: string;
      body: string;
    }
  | {
      type: "acceptStage";
      context: WizardDocContext;
    }
  | {
      type: "startCoding";
      context: WizardDocContext;
    }
  | {
      type: "runReview";
      context: WizardDocContext;
    }
  | {
      type: "promoteTodos";
      context: WizardDocContext;
    }
  | {
      type: "reopenChunk";
      context: WizardDocContext;
      chunkId: string;
    };

type AcceptedChunkSummary = {
  id: string;
  title: string;
  body: string;
};

type WizardEditorViewModel = {
  context: WizardDocContext;
  chunkTitle: string;
  guidance: string;
  body: string;
  chunkStatus: string;
  chunkId: string;
  hasDraft: boolean;
  allChunksAccepted: boolean;
  stageStatus: string;
  stageKind: string;
  stageTitle: string;
  stageIndex: number;
  stageCount: number;
  questions: string[];
  acceptedChunks: AcceptedChunkSummary[];
};

export class WizardEditorPanel implements vscode.Disposable {
  private readonly _actionEmitter = new vscode.EventEmitter<WizardEditorAction>();
  private readonly _disposables: vscode.Disposable[] = [];
  private readonly _extensionUri: vscode.Uri;
  private _panel: vscode.WebviewPanel | null = null;
  private _viewModel: WizardEditorViewModel | null = null;
  private _draftBody = "";

  readonly onDidTriggerAction = this._actionEmitter.event;

  constructor(extensionUri: vscode.Uri) {
    this._extensionUri = extensionUri;
  }

  async showResponse(result: WizardResponse): Promise<void> {
    if (!result.wizard) {
      return;
    }
    const viewModel = this._buildViewModel(result.wizard, result.openDocPath);
    if (!viewModel) {
      return;
    }

    this._viewModel = viewModel;
    this._draftBody = viewModel.body;
    const panel = this._ensurePanel();
    panel.title = `WaterFree: ${viewModel.chunkTitle}`;
    await panel.webview.postMessage({ type: "state", state: viewModel });
    panel.reveal(vscode.ViewColumn.One, false);
  }

  currentContext(): WizardDocContext | null {
    return this._viewModel?.context ?? null;
  }

  currentDraftForContext(ctx: WizardDocContext): string | null {
    if (
      !this._viewModel
      || this._viewModel.context.runId !== ctx.runId
      || this._viewModel.context.stageId !== ctx.stageId
      || this._viewModel.context.wizardId !== ctx.wizardId
    ) {
      return null;
    }
    return this._draftBody;
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
      "waterfreeWizardEditor",
      "WaterFree Wizard",
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [
          vscode.Uri.joinPath(this._extensionUri, "src", "ui", "wizard-editor"),
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

    if (message.type === "draftChanged" && typeof message.body === "string") {
      this._draftBody = message.body;
      if (this._viewModel) {
        this._viewModel = { ...this._viewModel, body: message.body };
      }
      return;
    }

    if (!this._viewModel) {
      return;
    }

    const body = typeof message.body === "string" ? message.body : this._draftBody;
    if (message.type === "generate") {
      this._actionEmitter.fire({
        type: "generate",
        context: this._viewModel.context,
        body,
      });
      return;
    }
    if (message.type === "refine") {
      this._actionEmitter.fire({
        type: "refine",
        context: this._viewModel.context,
        body,
      });
      return;
    }
    if (message.type === "acceptChunk" && typeof message.chunkId === "string") {
      this._actionEmitter.fire({
        type: "acceptChunk",
        context: this._viewModel.context,
        chunkId: message.chunkId,
        body,
      });
      return;
    }
    if (message.type === "acceptStage") {
      this._actionEmitter.fire({
        type: "acceptStage",
        context: this._viewModel.context,
      });
      return;
    }
    if (message.type === "startCoding") {
      this._actionEmitter.fire({
        type: "startCoding",
        context: this._viewModel.context,
      });
      return;
    }
    if (message.type === "runReview") {
      this._actionEmitter.fire({
        type: "runReview",
        context: this._viewModel.context,
      });
      return;
    }
    if (message.type === "promoteTodos") {
      this._actionEmitter.fire({
        type: "promoteTodos",
        context: this._viewModel.context,
      });
      return;
    }
    if (message.type === "reopenChunk" && typeof message.chunkId === "string") {
      this._actionEmitter.fire({
        type: "reopenChunk",
        context: this._viewModel.context,
        chunkId: message.chunkId,
      });
      return;
    }
  }

  private _buildViewModel(wizard: WizardRunData, _docPath: string): WizardEditorViewModel | null {
    const stage = wizard.stages.find((item) => item.id === wizard.currentStageId) ?? wizard.stages[0];
    if (!stage) {
      return null;
    }
    const chunk = this._currentChunk(stage);
    if (!chunk) {
      return null;
    }

    const allChunksAccepted = stage.chunks.every((c) => c.status === "accepted");
    const hasDraft = Boolean(chunk.draftText?.trim() || chunk.acceptedText?.trim());

    const acceptedChunks: AcceptedChunkSummary[] = stage.chunks
      .filter((c) => c.status === "accepted" && c.id !== chunk.id)
      .map((c) => ({
        id: c.id,
        title: c.title ?? "",
        body: this._chunkBody(c),
      }));

    const stageIndex = wizard.stages.indexOf(stage);

    return {
      context: {
        runId: wizard.id,
        stageId: stage.id,
        wizardId: wizard.wizardId,
        title: stage.title,
      },
      chunkTitle: chunk.title,
      guidance: chunk.guidance?.trim() ?? "",
      body: this._chunkBody(chunk),
      chunkStatus: chunk.status ?? "draft",
      chunkId: chunk.id,
      hasDraft,
      allChunksAccepted,
      stageStatus: stage.status ?? "pending",
      stageKind: stage.kind ?? "",
      stageTitle: stage.title ?? "",
      stageIndex,
      stageCount: wizard.stages.length,
      questions: stage.questions ?? [],
      acceptedChunks,
    };
  }

  private _currentChunk(stage: WizardStageData): WizardChunkData | null {
    for (const chunk of stage.chunks) {
      if (chunk.status !== "accepted") {
        return chunk;
      }
    }
    return stage.chunks[stage.chunks.length - 1] ?? null;
  }

  private _chunkBody(chunk: WizardChunkData): string {
    const accepted = this._sanitizeVisibleBody(chunk.acceptedText ?? "");
    if (accepted) {
      return accepted;
    }
    const draft = this._sanitizeVisibleBody(chunk.draftText ?? "");
    if (draft) {
      return draft;
    }
    return this._sanitizeVisibleBody(chunk.notesSnapshot ?? "");
  }

  private _sanitizeVisibleBody(value: string): string {
    let text = value.replace(/\r\n/g, "\n").trim();
    if (!text) {
      return "";
    }

    text = text
      .replace(/<!--[\s\S]*?-->/g, "")
      .replace(/^Status:\s*.*$/gim, "")
      .replace(/^###\s+Working Notes\s*$/gim, "")
      .replace(/^###\s+Latest Draft\s*$/gim, "")
      .replace(/^###\s+Accepted Output\s*$/gim, "")
      .replace(/^_Run the stage to generate this chunk\._$/gim, "")
      .replace(/^_Not accepted yet\._$/gim, "")
      .replace(/^#\s+What is your idea\?\s*\(describe in detail\)\s*$/gim, "")
      .trim();

    if (/^wf:/im.test(text) || /^waterfreeWizard:/im.test(text) || /^wizardId:/im.test(text)) {
      return "";
    }

    return text.replace(/\n{3,}/g, "\n\n").trim();
  }

  private _getHtml(webview: vscode.Webview): string {
    const nonce = getNonce();
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "wizard-editor", "wizard-editor.css"),
    );
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this._extensionUri, "src", "ui", "wizard-editor", "wizard-editor.js"),
    );
    const htmlPath = vscode.Uri.joinPath(
      this._extensionUri,
      "src",
      "ui",
      "wizard-editor",
      "wizard-editor.html",
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
