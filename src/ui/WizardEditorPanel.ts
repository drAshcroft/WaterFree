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
    }
  | {
      type: "useResearch";
      context: WizardDocContext;
      body: string;
    }
  | {
      type: "skipToArchitect";
      context: WizardDocContext;
    };

type AcceptedChunkSummary = {
  id: string;
  title: string;
  body: string;
};

type WizardIntakeOption = {
  value: string;
  label: string;
};

type WizardIntakeField = {
  id: string;
  label: string;
  placeholder?: string;
  remember?: boolean;
  options: WizardIntakeOption[];
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
  intakeFields: WizardIntakeField[];
  intakeAnswers: Record<string, string>;
  questions: string[];
  acceptedChunks: AcceptedChunkSummary[];
  externalResearchPrompt: string;
};

const INTAKE_PREFERENCE_KEYS: Record<string, string> = {
  teamSize: "waterfree.wizard.intake.teamSize",
  skillLevel: "waterfree.wizard.intake.skillLevel",
};

const LEGACY_INTAKE_VALUES: Record<string, Record<string, string>> = {
  teamSize: {
    solo: "Solo",
    "2_3": "2-3 people",
    "4_8": "4-8 people",
    "9_20": "9-20 people",
    "21_plus": "21+ people",
  },
  skillLevel: {
    new_to_software: "New to software",
    beginner: "Beginner builder",
    intermediate: "Intermediate",
    professional: "Professional engineer",
    expert: "Expert / specialist team",
  },
};

export class WizardEditorPanel implements vscode.Disposable {
  private readonly _actionEmitter = new vscode.EventEmitter<WizardEditorAction>();
  private readonly _disposables: vscode.Disposable[] = [];
  private readonly _extensionUri: vscode.Uri;
  private readonly _storage: vscode.Memento;
  private _panel: vscode.WebviewPanel | null = null;
  private _viewModel: WizardEditorViewModel | null = null;
  private _draftBody = "";

  readonly onDidTriggerAction = this._actionEmitter.event;

  constructor(extensionUri: vscode.Uri, storage: vscode.Memento) {
    this._extensionUri = extensionUri;
    this._storage = storage;
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

    if (message.type === "rememberIntakeDefaults" && isRecord(message.values)) {
      const nextAnswers = this._viewModel ? { ...this._viewModel.intakeAnswers } : {};
      for (const [fieldId, key] of Object.entries(INTAKE_PREFERENCE_KEYS)) {
        const value = message.values[fieldId];
        if (typeof value !== "string") {
          continue;
        }
        const normalized = value.trim();
        nextAnswers[fieldId] = normalized;
        void this._storage.update(key, normalized);
      }
      if (this._viewModel) {
        this._viewModel = { ...this._viewModel, intakeAnswers: nextAnswers };
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
    if (message.type === "useResearch") {
      this._actionEmitter.fire({
        type: "useResearch",
        context: this._viewModel.context,
        body,
      });
      return;
    }
    if (message.type === "skipToArchitect") {
      this._actionEmitter.fire({
        type: "skipToArchitect",
        context: this._viewModel.context,
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
    const intakeFields = this._buildIntakeFields(stage, chunk);

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
      intakeFields,
      intakeAnswers: this._loadIntakeAnswers(intakeFields),
      questions: stage.questions ?? [],
      acceptedChunks,
      externalResearchPrompt: stage.externalResearchPrompt?.trim() ?? "",
    };
  }

  private _buildIntakeFields(stage: WizardStageData, chunk: WizardChunkData): WizardIntakeField[] {
    if (stage.kind !== "market_research" || chunk.id !== "initial_goal") {
      return [];
    }
    return [
      {
        id: "whoFor",
        label: "Who is this for?",
        placeholder: "Choose the closest fit",
        options: [
          { value: "Self / personal use", label: "Self / personal use" },
          { value: "Home / household", label: "Home / household" },
          { value: "Internal team tool", label: "Internal team tool" },
          { value: "Small business / local service", label: "Small business / local service" },
          { value: "SaaS product", label: "SaaS product" },
          { value: "Startup venture", label: "Startup venture" },
          { value: "Agency / client delivery", label: "Agency / client delivery" },
          { value: "Creator / community product", label: "Creator / community product" },
          { value: "Ecommerce / marketplace", label: "Ecommerce / marketplace" },
          { value: "Education / training", label: "Education / training" },
          { value: "Open source utility", label: "Open source utility" },
          { value: "Game mod / server community", label: "Game mod / server community" },
        ],
      },
      {
        id: "teamSize",
        label: "How big is your team?",
        placeholder: "Select team size",
        remember: true,
        options: [
          { value: "Solo", label: "Solo" },
          { value: "2-3 people", label: "2-3 people" },
          { value: "4-8 people", label: "4-8 people" },
          { value: "9-20 people", label: "9-20 people" },
          { value: "21+ people", label: "21+ people" },
        ],
      },
      {
        id: "skillLevel",
        label: "What is your skill level?",
        placeholder: "Select skill level",
        remember: true,
        options: [
          { value: "New to software", label: "New to software" },
          { value: "Beginner builder", label: "Beginner builder" },
          { value: "Intermediate", label: "Intermediate" },
          { value: "Professional engineer", label: "Professional engineer" },
          { value: "Expert / specialist team", label: "Expert / specialist team" },
        ],
      },
      {
        id: "startingPoint",
        label: "What are you starting from?",
        placeholder: "Select current state",
        options: [
          { value: "Just an idea", label: "Just an idea" },
          { value: "Rough notes or sketches", label: "Rough notes or sketches" },
          { value: "Existing manual workflow", label: "Existing manual workflow" },
          { value: "Prototype already exists", label: "Prototype already exists" },
          { value: "Existing product to expand", label: "Existing product to expand" },
        ],
      },
      {
        id: "firstWin",
        label: "What should the first win be?",
        placeholder: "Choose or type the best fit",
        options: [
          { value: "Launch a web MVP", label: "Launch a web MVP" },
          { value: "Launch a mobile MVP", label: "Launch a mobile MVP" },
          { value: "Ship a desktop tool", label: "Ship a desktop tool" },
          { value: "Ship an API / backend service", label: "Ship an API / backend service" },
          { value: "Ship an automation / AI agent", label: "Ship an automation / AI agent" },
          { value: "Ship a plugin / extension", label: "Ship a plugin / extension" },
          { value: "Ship a game mod / community plugin", label: "Ship a game mod / community plugin" },
          { value: "Validate demand fast", label: "Validate demand fast" },
          { value: "Get first users or customers", label: "Get first users or customers" },
          { value: "Save time / remove busywork", label: "Save time / remove busywork" },
          { value: "Learn while building", label: "Learn while building" },
          { value: "Create a strong demo / portfolio piece", label: "Create a strong demo / portfolio piece" },
        ],
      },
    ];
  }

  private _loadIntakeAnswers(fields: WizardIntakeField[]): Record<string, string> {
    const answers: Record<string, string> = {};
    for (const field of fields) {
      if (!field.remember) {
        continue;
      }
      const key = INTAKE_PREFERENCE_KEYS[field.id];
      if (!key) {
        continue;
      }
      const saved = this._storage.get<string>(key, "").trim();
      const normalized = LEGACY_INTAKE_VALUES[field.id]?.[saved] ?? saved;
      if (normalized) {
        answers[field.id] = normalized;
      }
    }
    return answers;
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
