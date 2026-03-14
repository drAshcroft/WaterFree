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
};

const INTAKE_PREFERENCE_KEYS: Record<string, string> = {
  teamSize: "waterfree.wizard.intake.teamSize",
  skillLevel: "waterfree.wizard.intake.skillLevel",
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
          { value: "self", label: "Self / personal use" },
          { value: "home", label: "Home / household" },
          { value: "internal_tool", label: "Internal team tool" },
          { value: "small_business", label: "Small business / local service" },
          { value: "saas", label: "SaaS product" },
          { value: "startup", label: "Startup venture" },
          { value: "client_service", label: "Agency / client delivery" },
          { value: "creator", label: "Creator / community product" },
          { value: "ecommerce", label: "Ecommerce / marketplace" },
          { value: "education", label: "Education / training" },
          { value: "open_source", label: "Open source utility" },
          { value: "game_mod", label: "Game mod / server community" },
        ],
      },
      {
        id: "teamSize",
        label: "How big is your team?",
        placeholder: "Select team size",
        remember: true,
        options: [
          { value: "solo", label: "Solo" },
          { value: "2_3", label: "2-3 people" },
          { value: "4_8", label: "4-8 people" },
          { value: "9_20", label: "9-20 people" },
          { value: "21_plus", label: "21+ people" },
        ],
      },
      {
        id: "skillLevel",
        label: "What is your skill level?",
        placeholder: "Select skill level",
        remember: true,
        options: [
          { value: "new_to_software", label: "New to software" },
          { value: "beginner", label: "Beginner builder" },
          { value: "intermediate", label: "Intermediate" },
          { value: "professional", label: "Professional engineer" },
          { value: "expert", label: "Expert / specialist team" },
        ],
      },
      {
        id: "startingPoint",
        label: "What are you starting from?",
        placeholder: "Select current state",
        options: [
          { value: "idea_only", label: "Just an idea" },
          { value: "notes", label: "Rough notes or sketches" },
          { value: "manual_process", label: "Existing manual workflow" },
          { value: "prototype", label: "Prototype already exists" },
          { value: "existing_product", label: "Existing product to expand" },
        ],
      },
      {
        id: "primaryPlatform",
        label: "What do you want to ship first?",
        placeholder: "Select first target",
        options: [
          { value: "web_app", label: "Web app" },
          { value: "mobile_app", label: "Mobile app" },
          { value: "desktop_app", label: "Desktop app" },
          { value: "api_backend", label: "API / backend service" },
          { value: "automation_agent", label: "Automation / AI agent" },
          { value: "integration_plugin", label: "Integration / plugin" },
          { value: "vscode_extension", label: "VS Code extension" },
          { value: "game_mod", label: "Game mod / plugin" },
        ],
      },
      {
        id: "timeline",
        label: "What timeline are you targeting?",
        placeholder: "Select timeline",
        options: [
          { value: "weekend", label: "A weekend prototype" },
          { value: "few_weeks", label: "2-6 weeks" },
          { value: "quarter", label: "1-3 months" },
          { value: "longer", label: "3+ months" },
          { value: "no_deadline", label: "No hard deadline yet" },
        ],
      },
      {
        id: "successMetric",
        label: "What matters most right now?",
        placeholder: "Select main goal",
        options: [
          { value: "save_time", label: "Save time / remove busywork" },
          { value: "validate_demand", label: "Validate real demand" },
          { value: "get_users", label: "Get first users or customers" },
          { value: "ship_mvp", label: "Ship the fastest MVP" },
          { value: "learn", label: "Learn while building" },
          { value: "portfolio", label: "Create a strong demo / portfolio piece" },
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
      if (saved) {
        answers[field.id] = saved;
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
