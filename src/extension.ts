/**
 * WaterFree VS Code Extension — entry point.
 *
 * Responsibilities:
 *  - Spawn and manage the Python backend process (PythonBridge)
 *  - Register all VS Code commands
 *  - Wire UI components: PlanSidebar, StatusBarManager, DecorationRenderer
 *  - Handle file watching (FileWatcher) and TODO detection (TodoWatcher)
 *  - Route user actions to the Python backend
 */

import * as path from "path";
import * as vscode from "vscode";

import {
  PythonBridge,
  type WizardResponse,
  type WizardRunData,
  type WizardStageData,
} from "./bridge/PythonBridge.js";
import { LiveDebugCapture } from "./debug/LiveDebugCapture.js";
import { PairLogger } from "./logging/PairLogger.js";
import { WaterFreeSecrets } from "./security/WaterFreeSecrets.js";
import { StatusBarManager } from "./ui/StatusBarManager.js";
import {
  type BacklogSummaryData,
  PlanSidebarProvider,
  getAnnotationTargetLine,
  getTaskTargetLine,
  getTaskTargetPath,
  type PlanData,
  type SidebarAction,
  type TaskData,
} from "./ui/PlanSidebar.js";
import { WizardCodeLensProvider, type WizardDocContext } from "./ui/WizardCodeLensProvider.js";
import { QuickActionsProvider } from "./ui/QuickActionsProvider.js";
import { DecorationRenderer } from "./ui/DecorationRenderer.js";
import { WizardEditorPanel, type WizardEditorAction } from "./ui/WizardEditorPanel.js";
import { FileWatcher } from "./watchers/FileWatcher.js";
import { TodoWatcher, type WfTodo } from "./watchers/TodoWatcher.js";
import { isWizardDoc, isWizardDocPath, parseWizardDocContextFromDocument } from "./wizard/WizardDocState.js";
import { CommandRegistry } from "./commands/CommandRegistry.js";

// ------------------------------------------------------------------
// Extension state (singleton per extension host)
// ------------------------------------------------------------------

let controller: WaterFreeController | null = null;

type IndexWorkspaceResult = {
  indexed?: boolean;
  status?: string;
  reason?: string;
  changedCount?: number;
  changedPaths?: string[];
  dbPath?: string;
  scannedFiles?: number;
};

type ExecutionDiagnostic = {
  file: string;
  line: number;
  severity: "error" | "warning" | "info" | "hint";
  source: string;
  message: string;
};

type FinalizeExecutionResult = {
  ok: boolean;
  status: string;
  blockingDiagnostics: ExecutionDiagnostic[];
};

export function activate(context: vscode.ExtensionContext): void {
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  if (!workspaceRoot) {
    return; // No workspace open — nothing to do
  }

  controller = new WaterFreeController(workspaceRoot, context);
  context.subscriptions.push(controller);
}

export function deactivate(): void {
  controller?.dispose();
  controller = null;
}

// ------------------------------------------------------------------
// Controller — owns all subsystems and routes commands
// ------------------------------------------------------------------

export class WaterFreeController implements vscode.Disposable {
  private readonly _logger: PairLogger;
  private readonly _bridge: PythonBridge;
  private readonly _secrets: WaterFreeSecrets;
  private readonly _statusBar: StatusBarManager;
  private readonly _sidebarProvider: PlanSidebarProvider;
  private readonly _quickActions: QuickActionsProvider;
  private readonly _decorations: DecorationRenderer;
  private readonly _wizardLenses: WizardCodeLensProvider;
  private readonly _wizardEditor: WizardEditorPanel;
  private readonly _fileWatcher: FileWatcher;
  private readonly _todoWatcher: TodoWatcher;
  private readonly _liveDebug: LiveDebugCapture;
  private readonly _disposables: vscode.Disposable[] = [];

  private _sessionId: string | null = null;
  private _plan: PlanData | null = null;

  constructor(
    private readonly _workspacePath: string,
    context: vscode.ExtensionContext,
  ) {
    const logFilePath = path.join(_workspacePath, ".waterfree", "logs", "extension.log");
    this._logger = new PairLogger(logFilePath);
    this._disposables.push(this._logger);

    // Core subsystems
    this._secrets = new WaterFreeSecrets(context, context.extensionUri.fsPath);
    this._bridge = new PythonBridge(_workspacePath, context.extensionUri.fsPath, this._logger);
    this._statusBar = new StatusBarManager();
    this._sidebarProvider = new PlanSidebarProvider(context.extensionUri);
    this._quickActions = new QuickActionsProvider();
    this._decorations = new DecorationRenderer();
    this._wizardLenses = new WizardCodeLensProvider();
    this._wizardEditor = new WizardEditorPanel(context.extensionUri);
    this._fileWatcher = new FileWatcher(this._bridge, _workspacePath);
    this._todoWatcher = new TodoWatcher();
    this._liveDebug = new LiveDebugCapture();
    this._disposables.push(
      this._bridge,
      this._statusBar,
      this._sidebarProvider,
      this._quickActions,
      this._decorations,
      this._wizardLenses,
      this._wizardEditor,
      this._fileWatcher,
      this._todoWatcher,
      this._liveDebug,
    );

    // Sidebar views
    const planView = vscode.window.registerWebviewViewProvider(
      "waterfree.planSidebar",
      this._sidebarProvider,
      {
        webviewOptions: {
          retainContextWhenHidden: true,
        },
      },
    );
    const quickActionsView = vscode.window.createTreeView("waterfree.quickActions", {
      treeDataProvider: this._quickActions,
    });
    this._disposables.push(planView, quickActionsView);

    // Bridge notifications
    this._disposables.push(
      this._bridge.onNotification("indexProgress", (params) => {
        const p = params as { done: number; total: number };
        this._statusBar.setState("idle");
        this._log("index", `${p.done}/${p.total} files`);
      }),
      this._bridge.onNotification("sessionUpdate", (params) => {
        this._onSessionUpdate(params as PlanData);
      }),
      this._sidebarProvider.onDidTriggerAction((action) => {
        void this._onSidebarAction(action);
      }),
      this._wizardEditor.onDidTriggerAction((action) => {
        void this._onWizardEditorAction(action);
      }),
    );

    // TODO watcher
    this._todoWatcher.onTodosFound((todos) => this._onTodosFound(todos));

    // Debug session state — update sidebar when debug starts/stops
    this._disposables.push(
      vscode.debug.onDidStartDebugSession(() => this._updateDebugState()),
      vscode.debug.onDidTerminateDebugSession(() => this._updateDebugState()),
      vscode.window.onDidChangeActiveTextEditor((editor) => this._updateWizardEditorContext(editor)),
      vscode.workspace.onDidOpenTextDocument((document) => {
        if (vscode.window.activeTextEditor?.document === document) {
          this._updateWizardEditorContext(vscode.window.activeTextEditor);
        }
      }),
    );

    // Commands
    const commandRegistry = new CommandRegistry(this);
    commandRegistry.register(context);

    // Start the backend and auto-index
    this._log("startup", `workspace=${this._workspacePath}`);
    this._log("startup", `extension log file=${this._logger.logFilePath}`);
    this._updateWizardEditorContext(vscode.window.activeTextEditor);

    // Try to resume an existing session
    this._sidebarProvider.setCheckingForSession(true);
    void this._bootstrap();
  }

  // ------------------------------------------------------------------
  // Accessor for QuickActionsProvider (used by CommandRegistry)
  // ------------------------------------------------------------------

  getQuickActions(): QuickActionsProvider {
    return this._quickActions;
  }

  // ------------------------------------------------------------------
  // Public command methods (called by CommandRegistry)
  // ------------------------------------------------------------------

  async cmdSetup(): Promise<void> {
    const configured = await this._secrets.promptForAnthropicApiKey();
    if (!configured) {
      return;
    }

    this._bridge.setAnthropicApiKey(this._secrets.anthropicApiKey);
    this._bridge.restart();
    void vscode.window.showInformationMessage("WaterFree: API key stored securely.");
  }

  async cmdStart(goalOverride?: string, personaOverride?: string): Promise<void> {
    const goal = goalOverride ?? await vscode.window.showInputBox({
      prompt: "What do you want to build or fix?",
      placeHolder: "e.g. Add user authentication using JWT tokens",
      validateInput: (v) => (v.trim() ? null : "Please describe the goal."),
    });
    if (!goal) {
      return;
    }

    const trimmedGoal = goal.trim();
    this._statusBar.setState("planning");
    this._sidebarProvider.setBusyMessage("Planning session...");
    try {
      const session = await this._bridge.request<PlanData>("createSession", {
        goal: trimmedGoal,
        workspacePath: this._workspacePath,
        persona: personaOverride ?? "default",
      });
      this._sessionId = session.id;

      // Index if not done, then generate plan
      await this.cmdIndex(false);

      const result = await this._bridge.request<{
        sessionId: string;
        tasks: PlanData["tasks"];
        questions: string[];
      }>("generatePlan", {
        goal: trimmedGoal,
        sessionId: this._sessionId,
        workspacePath: this._workspacePath,
      });

      this._sessionId = result.sessionId;

      if (result.questions.length > 0) {
        const answer = await vscode.window.showInformationMessage(
          `Before planning, the AI has a question:\n${result.questions[0]}`,
          "Continue anyway",
          "Cancel",
        );
        if (answer !== "Continue anyway") {
          this._statusBar.setState("idle");
          return;
        }
      }

      this._onPlanReceived({ ...session, tasks: result.tasks });
      this._sidebarProvider.clearComposer();
    } catch (err) {
      this._handleError("Start session failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdIndex(showResult = true): Promise<void> {
    this._statusBar.setState("scanning");
    try {
      const result = await this._bridge.request<IndexWorkspaceResult>("indexWorkspace", {
        path: this._workspacePath,
      });
      this._statusBar.setState("idle");

      if (!showResult) {
        return;
      }

      if (result.indexed) {
        const changed = result.changedCount ?? 0;
        const scanCount = result.scannedFiles ?? 0;
        void vscode.window.showInformationMessage(
          `WaterFree: Index refreshed (${changed} changed file(s), ${scanCount} scanned).`,
        );
      } else {
        const scanCount = result.scannedFiles ?? 0;
        void vscode.window.showInformationMessage(
          `WaterFree: Index is up to date (${scanCount} file(s) checked).`,
        );
      }
    } catch (err) {
      this._handleError("Indexing failed", err);
    }
  }

  async cmdGenerateAnnotation(taskId: string): Promise<void> {
    if (!this._sessionId) {
      void vscode.window.showWarningMessage("No active session.");
      return;
    }
    this._statusBar.setState("annotating");
    this._sidebarProvider.setBusyMessage("Generating intent...");
    try {
      const annotation = await this._bridge.request("generateAnnotation", {
        taskId,
        sessionId: this._sessionId,
      });
      this._log("annotation", `generated: ${JSON.stringify(annotation)}`);
      // Reload session to pick up the new annotation
      await this._reloadSession();
      this._statusBar.setState("awaiting_review");
    } catch (err) {
      this._handleError("Annotation failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdApprove(annotationId: string): Promise<void> {
    if (!this._sessionId) {
      return;
    }

    // Find the task that owns this annotation — needed for executeTask
    const task = this._plan?.tasks.find((t) =>
      t.annotations.some((a) => a.id === annotationId),
    );
    if (!task) {
      this._handleError("Approve failed", new Error("Could not resolve task for annotation"));
      return;
    }

    this._statusBar.setState("executing");
    this._sidebarProvider.setBusyMessage("Executing task...");
    try {
      // Step 1 — mark annotation approved in the session
      await this._bridge.request("approveAnnotation", {
        annotationId,
        sessionId: this._sessionId,
      });

      // Step 2 — ask Claude to generate the code edits and apply them
      await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `WaterFree: Executing "${task.title}"…`,
          cancellable: false,
        },
        async () => {
          const result = await this._bridge.request<{
            edits: Array<{ targetFile: string; newContent: string; explanation?: string }>;
            taskId: string;
          }>("executeTask", {
            taskId: task.id,
            sessionId: this._sessionId!,
          });

          try {
            const touchedFiles = await this._applyEdits(result.edits);
            const diagnostics = await this._collectDiagnostics(touchedFiles);
            const finalized = await this._bridge.request<FinalizeExecutionResult>("finalizeExecution", {
              sessionId: this._sessionId!,
              taskId: task.id,
              diagnostics,
            });
            await this._reloadSession();

            if (finalized.ok) {
              this._statusBar.setState("idle");
              void vscode.window.showInformationMessage(
                `WaterFree: "${task.title}" executed successfully.`,
              );
              return;
            }

            this._statusBar.setState("awaiting_review");
            const first = finalized.blockingDiagnostics[0];
            const location = first ? `${path.basename(first.file)}:${first.line}` : "edited files";
            const message = first?.message ?? "Blocking diagnostics remain.";
            void vscode.window.showWarningMessage(
              `WaterFree: "${task.title}" needs review — ${location}: ${message}`,
            );
          } catch (err) {
            await this._bridge.request("finalizeExecution", {
              sessionId: this._sessionId!,
              taskId: task.id,
              diagnostics: [{
                file: "",
                line: 0,
                severity: "error",
                source: "editor",
                message: err instanceof Error ? err.message : String(err),
              }],
            }).catch(() => undefined);
            throw err;
          }
        },
      );

    } catch (err) {
      this._statusBar.setState("idle");
      this._handleError("Execution failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdAlter(taskId: string, annotationId: string, feedbackOverride?: string): Promise<void> {
    if (!this._sessionId) {
      return;
    }
    const feedback = feedbackOverride ?? await vscode.window.showInputBox({
      prompt: "What should be different?",
      placeHolder: "e.g. Don't touch the authentication middleware",
    });
    if (!feedback) {
      return;
    }
    this._statusBar.setState("annotating");
    this._sidebarProvider.setBusyMessage("Updating intent...");
    try {
      await this._bridge.request("alterAnnotation", {
        taskId,
        annotationId,
        feedback: feedback.trim(),
        sessionId: this._sessionId,
      });
      await this._reloadSession();
      this._statusBar.setState("awaiting_review");
    } catch (err) {
      this._handleError("Alter failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdRedirect(taskId: string, instructionOverride?: string): Promise<void> {
    if (!this._sessionId) {
      return;
    }
    const instruction = instructionOverride ?? await vscode.window.showInputBox({
      prompt: "Give a new direction for this task:",
      placeHolder: "e.g. Instead of modifying X, create a new Y",
    });
    if (!instruction) {
      return;
    }
    this._statusBar.setState("awaiting_redirect");
    this._sidebarProvider.setBusyMessage("Redirecting task...");
    try {
      await this._bridge.request("redirectTask", {
        taskId,
        instruction: instruction.trim(),
        sessionId: this._sessionId,
      });
      await this._reloadSession();
      this._statusBar.setState("idle");
    } catch (err) {
      this._handleError("Redirect failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdSkipTask(taskId: string): Promise<void> {
    if (!this._sessionId) {
      return;
    }
    this._sidebarProvider.setBusyMessage("Skipping task...");
    try {
      await this._bridge.request("skipTask", { taskId, sessionId: this._sessionId });
      await this._reloadSession();
    } catch (err) {
      this._handleError("Skip failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdBuildKnowledge(): Promise<void> {
    const focus = await vscode.window.showInputBox({
      prompt: "What do you want to extract? (optional — leave blank for general patterns)",
      placeHolder: "e.g. authentication patterns, error handling, Django ORM usage",
    });
    if (focus === undefined) {
      return; // User pressed Escape
    }

    const busyMsg = focus.trim()
      ? `Snippetizing: "${focus.trim()}"...`
      : "Snippetizing workspace...";
    this._sidebarProvider.setBusyMessage(busyMsg);
    try {
      const result = await this._bridge.request<{
        added: number;
        symbolsScanned: number;
        repo: string;
        message: string;
      }>("buildKnowledge", { workspacePath: this._workspacePath, focus: focus.trim() });
      void vscode.window.showInformationMessage(
        `WaterFree Snippetize: ${result.message ?? `Added ${result.added} snippets from workspace.`}`,
      );
    } catch (err) {
      this._handleError("Pair Snippetize failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdAddKnowledgeRepo(): Promise<void> {
    const source = await vscode.window.showInputBox({
      prompt: "WaterFree Snippetize: Git repo URL or local path to snippetize",
      placeHolder: "https://github.com/org/repo.git  or  /path/to/local/repo",
      validateInput: (v) => (v.trim() ? null : "Enter a URL or local path."),
    });
    if (!source) {
      return;
    }

    const focus = await vscode.window.showInputBox({
      prompt: "What do you want to extract from this repo? (optional)",
      placeHolder: "e.g. React hooks patterns, authentication, data validation",
    });
    if (focus === undefined) {
      return; // User pressed Escape
    }

    this._sidebarProvider.setBusyMessage("Snippetizing repo...");
    try {
      const result = await this._bridge.request<{
        added: number;
        symbolsScanned: number;
        name: string;
        error?: string;
      }>("addKnowledgeRepo", { source: source.trim(), focus: focus.trim() });

      if (result.error) {
        void vscode.window.showErrorMessage(`WaterFree Snippetize: Failed — ${result.error}`);
      } else {
        void vscode.window.showInformationMessage(
          `WaterFree Snippetize: '${result.name}' — ${result.added} snippets added (${result.symbolsScanned ?? 0} symbols scanned).`,
        );
      }
    } catch (err) {
      this._handleError("Pair Snippetize repo failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  cmdExtractProcedure(): void {
    const editor = vscode.window.activeTextEditor;
    let symbol = "";
    if (editor) {
      if (!editor.selection.isEmpty) {
        symbol = editor.document.getText(editor.selection).trim();
      } else {
        const wordRange = editor.document.getWordRangeAtPosition(editor.selection.active);
        if (wordRange) {
          symbol = editor.document.getText(wordRange);
        }
      }
    }
    this._sidebarProvider.prefillSnippetize(symbol);
    void vscode.commands.executeCommand("waterfree.planSidebar.focus");
  }

  async cmdOpenWizard(args?: unknown): Promise<void> {
    const launch = this._parseWizardLaunchArgs(args);
    const wizardId = launch?.wizardId ?? "bring_idea_to_life";
    if (wizardId !== "bring_idea_to_life") {
      void vscode.window.showInformationMessage(
        `WaterFree: '${wizardId.replace(/_/g, " ")}' is not upgraded yet. Only Bring Idea to Life uses the markdown wizard flow right now.`,
      );
      return;
    }

    this._sidebarProvider.setBusyMessage("Opening wizard...");
    try {
      const config = vscode.workspace.getConfiguration("waterfree");
      const result = await this._bridge.createWizardSession({
        goal: launch?.goal?.trim() ?? "",
        wizardId,
        publicDocsPath: config.get<string>("publicDocsPath") ?? "docs",
        workspacePath: this._workspacePath,
        persona: launch?.persona ?? "architect",
      });
      await this._handleWizardResponse(result);
    } catch (err) {
      this._handleError("Open wizard failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdRunWizardStep(ctx?: unknown): Promise<void> {
    const wizard = await this._resolveWizardContext(ctx);
    if (!wizard) {
      return;
    }
    await this._saveWizardDocument(wizard);
    const extraContext = this._getWizardDraftContext(wizard);
    this._sidebarProvider.setBusyMessage("Running wizard stage...");
    try {
      const result = await this._bridge.runWizardStep({
        runId: wizard.runId,
        stageId: wizard.stageId,
        extraContext,
        workspacePath: this._workspacePath,
      });
      await this._handleWizardResponse(result);
    } catch (err) {
      this._handleError("Run wizard stage failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdAcceptWizardChunk(ctx?: unknown, chunkId?: unknown): Promise<void> {
    const wizard = await this._resolveWizardContext(ctx);
    if (!wizard || typeof chunkId !== "string" || !chunkId.trim()) {
      return;
    }
    await this._saveWizardDocument(wizard);
    this._sidebarProvider.setBusyMessage("Accepting chunk...");
    try {
      const result = await this._bridge.acceptWizardChunk({
        runId: wizard.runId,
        stageId: wizard.stageId,
        chunkId: chunkId.trim(),
        workspacePath: this._workspacePath,
      });
      await this._handleWizardResponse(result);
    } catch (err) {
      this._handleError("Accept chunk failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdReviseWizardChunk(ctx?: unknown, chunkId?: unknown): Promise<void> {
    const wizard = await this._resolveWizardContext(ctx);
    if (!wizard || typeof chunkId !== "string" || !chunkId.trim()) {
      return;
    }

    const revisionNote = await vscode.window.showInputBox({
      prompt: "What should change in this chunk?",
      placeHolder: "e.g. tighten the MVP and remove the enterprise assumptions",
    });
    if (revisionNote === undefined) {
      return;
    }

    await this._saveWizardDocument(wizard);
    this._sidebarProvider.setBusyMessage("Revising chunk...");
    try {
      const result = await this._bridge.runWizardStep({
        runId: wizard.runId,
        stageId: wizard.stageId,
        chunkId: chunkId.trim(),
        revisionNote: revisionNote.trim(),
        workspacePath: this._workspacePath,
      });
      await this._handleWizardResponse(result);
    } catch (err) {
      this._handleError("Revise chunk failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdAcceptWizardStep(ctx?: unknown): Promise<void> {
    const wizard = await this._resolveWizardContext(ctx);
    if (!wizard) {
      return;
    }
    await this._saveWizardDocument(wizard);
    this._sidebarProvider.setBusyMessage("Accepting stage...");
    try {
      const result = await this._bridge.acceptWizardStep({
        runId: wizard.runId,
        stageId: wizard.stageId,
        workspacePath: this._workspacePath,
      });
      await this._handleWizardResponse(result);
    } catch (err) {
      this._handleError("Accept stage failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdPromoteWizardTodos(ctx?: unknown): Promise<void> {
    const wizard = await this._resolveWizardContext(ctx);
    if (!wizard) {
      return;
    }
    this._sidebarProvider.setBusyMessage("Promoting wizard todos...");
    try {
      const result = await this._bridge.promoteWizardTodos({
        runId: wizard.runId,
        workspacePath: this._workspacePath,
      });
      await this._handleWizardResponse(result, false);
      const count = result.count ?? result.createdTaskIds?.length ?? 0;
      void vscode.window.showInformationMessage(`WaterFree: promoted ${count} wizard todo(s) to the backlog.`);
    } catch (err) {
      this._handleError("Promote wizard todos failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdStartWizardCoding(ctx?: unknown): Promise<void> {
    const wizard = await this._resolveWizardContext(ctx);
    if (!wizard) {
      return;
    }
    await this._saveWizardDocument(wizard);
    this._sidebarProvider.setBusyMessage("Starting coding handoff...");
    try {
      const result = await this._bridge.startWizardCoding({
        runId: wizard.runId,
        workspacePath: this._workspacePath,
      });
      await this._handleWizardResponse(result, false);
      void vscode.window.showInformationMessage("WaterFree: coding session created from accepted wizard outputs.");
    } catch (err) {
      this._handleError("Start wizard coding failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdRunWizardReview(ctx?: unknown): Promise<void> {
    const wizard = await this._resolveWizardContext(ctx);
    if (!wizard) {
      return;
    }
    await this._saveWizardDocument(wizard);
    this._sidebarProvider.setBusyMessage("Running review...");
    try {
      const result = await this._bridge.runWizardReview({
        runId: wizard.runId,
        workspacePath: this._workspacePath,
      });
      await this._handleWizardResponse(result);
    } catch (err) {
      this._handleError("Run wizard review failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  async cmdRefineWizardIdea(ctx?: unknown): Promise<void> {
    const wizard = await this._resolveWizardContext(ctx);
    if (!wizard) {
      return;
    }
    await this._saveWizardDocument(wizard);
    const extraContext = this._getWizardDraftContext(wizard);
    this._sidebarProvider.setBusyMessage("Getting clarifying questions...");
    try {
      const result = await this._bridge.runWizardStep({
        runId: wizard.runId,
        stageId: wizard.stageId,
        mode: "clarify",
        extraContext,
        workspacePath: this._workspacePath,
      });
      await this._handleWizardResponse(result);
      const stage = (result.wizard as WizardRunData | null)?.stages?.find(
        (s: WizardStageData) => s.id === wizard.stageId,
      );
      const questions: string[] = stage?.questions ?? [];
      if (questions.length > 0) {
        void vscode.window.showInformationMessage(
          "WaterFree: Clarifying Questions",
          { modal: true, detail: questions.map((q, i) => `${i + 1}. ${q}`).join("\n") },
          "OK",
        );
      }
    } catch (err) {
      this._handleError("Refine idea failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  cmdShowAnnotation(taskId: string, annotationId?: string): void {
    const task = this._plan?.tasks.find((t) => t.id === taskId);
    if (!task) {
      return;
    }

    const annotation = annotationId
      ? task.annotations.find((a) => a.id === annotationId)
      : task.annotations[task.annotations.length - 1];

    if (!annotation) {
      return;
    }

    // Show annotation detail in a quick-pick style panel
    const detail = annotation.detail ?? annotation.summary;
    void vscode.window.showInformationMessage(
      `[WF] ${annotation.summary}`,
      { detail, modal: true },
      "✓ Approve",
      "✎ Alter",
      "⟳ Redirect",
    ).then((choice) => {
      if (choice === "✓ Approve") {
        void this.cmdApprove(annotation.id);
      } else if (choice === "✎ Alter") {
        void this.cmdAlter(taskId, annotation.id);
      } else if (choice === "⟳ Redirect") {
        void this.cmdRedirect(taskId);
      }
    });

    // Navigate to the target file + line
    const targetFile = getTaskTargetPath(task, this._workspacePath);
    if (targetFile) {
      const targetLine = getAnnotationTargetLine(annotation, task);
      void vscode.window.showTextDocument(
        vscode.Uri.file(targetFile),
        { selection: new vscode.Range(targetLine, 0, targetLine, 0) },
      );
    }
  }

  async cmdLivePairDebug(): Promise<void> {
    if (!vscode.debug.activeDebugSession) {
      void vscode.window.showInformationMessage(
        "WaterFree: No active debug session. Start debugging first, then pause at a breakpoint.",
      );
      return;
    }

    this._statusBar.setState("answering");

    const ctx = await this._liveDebug.capture();
    if (!ctx) {
      void vscode.window.showWarningMessage(
        "WaterFree: Could not capture debug state. Try pausing at a breakpoint first.",
      );
      this._statusBar.setState("idle");
      return;
    }

    void vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "WaterFree: Analysing debug state…",
        cancellable: false,
      },
      async () => {
        try {
          const analysis = await this._bridge.request<{
            diagnosis: string;
            likelyCause: string;
            suggestedFix: {
              summary: string;
              detail: string;
              targetFile?: string;
              targetLine?: number;
              willModify?: string[];
              willCreate?: string[];
              sideEffectWarnings?: string[];
            };
            questions?: string[];
          }>("liveDebug", {
            debugContext: ctx,
            sessionId: this._sessionId ?? undefined,
            workspacePath: this._workspacePath,
          });

          this._statusBar.setState("awaiting_review");
          await this._showDebugAnalysis(analysis, ctx);
        } catch (err) {
          this._handleError("Live Pair Debug failed", err);
        }
      },
    );
  }

  async cmdPushDebugToAgent(intentOverride?: string, stopReasonOverride?: string): Promise<void> {
    if (!vscode.debug.activeDebugSession) {
      void vscode.window.showInformationMessage(
        "WaterFree: No active debug session. Start debugging and pause at a breakpoint first.",
      );
      return;
    }

    const intent = intentOverride ?? await vscode.window.showInputBox({
      prompt: "What do you want to investigate?",
      placeHolder: "e.g. Why is user.balance going negative?",
    });
    if (intent === undefined) {
      return;
    }

    const stopReason = stopReasonOverride ?? "other";

    this._sidebarProvider.setBusyMessage("Capturing debug state…");
    try {
      const snapshotPath = await this._liveDebug.writeSnapshot(intent, stopReason, this._workspacePath);
      void vscode.window.showInformationMessage(
        `WaterFree: Debug snapshot saved — agent can query it via mcp_debug tools. (${snapshotPath})`,
      );
    } catch (err) {
      this._handleError("Push debug to agent failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  // ------------------------------------------------------------------
  // Private helpers
  // ------------------------------------------------------------------

  private async _bootstrap(): Promise<void> {
    try {
      await this._secrets.initialize();
      await this._secrets.promptForSetupIfNeeded();
      this._bridge.setAnthropicApiKey(this._secrets.anthropicApiKey);
      this._bridge.start();
      await this._autoIndex();
      await this._tryResumeSession();
    } catch (err) {
      this._sidebarProvider.setCheckingForSession(false);
      this._handleError("Startup failed", err);
    }
  }

  private async _applyEdits(
    edits: Array<{ targetFile: string; newContent: string }>,
  ): Promise<string[]> {
    if (edits.length === 0) {
      return [];
    }

    const wsEdit = new vscode.WorkspaceEdit();
    const touchedFiles = Array.from(new Set(edits.map((edit) => edit.targetFile)));

    for (const edit of edits) {
      const uri = vscode.Uri.file(edit.targetFile);

      let doc: vscode.TextDocument | undefined;
      try {
        doc = await vscode.workspace.openTextDocument(uri);
      } catch {
        // File does not exist yet — create it
        wsEdit.createFile(uri, { ignoreIfExists: false });
        wsEdit.insert(uri, new vscode.Position(0, 0), edit.newContent);
        continue;
      }

      const fullRange = new vscode.Range(
        doc.positionAt(0),
        doc.positionAt(doc.getText().length),
      );
      wsEdit.replace(uri, fullRange, edit.newContent);
    }

    const ok = await vscode.workspace.applyEdit(wsEdit);
    if (!ok) {
      throw new Error("WorkspaceEdit failed — one or more edits could not be applied.");
    }

    // Save all modified documents so the file watcher picks them up
    for (const edit of edits) {
      try {
        const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(edit.targetFile));
        await doc.save();
      } catch {
        // Non-fatal — file may have been created but not yet tracked
      }
    }

    return touchedFiles;
  }

  private async _collectDiagnostics(touchedFiles: string[]): Promise<ExecutionDiagnostic[]> {
    if (touchedFiles.length === 0) {
      return [];
    }

    await new Promise((resolve) => setTimeout(resolve, 150));

    const diagnostics: ExecutionDiagnostic[] = [];
    for (const filePath of touchedFiles) {
      const uri = vscode.Uri.file(filePath);
      for (const diagnostic of vscode.languages.getDiagnostics(uri)) {
        diagnostics.push({
          file: filePath,
          line: diagnostic.range.start.line + 1,
          severity: this._diagnosticSeverity(diagnostic.severity),
          source: diagnostic.source ?? "",
          message: diagnostic.message,
        });
      }
    }
    return diagnostics;
  }

  private _diagnosticSeverity(severity: vscode.DiagnosticSeverity): ExecutionDiagnostic["severity"] {
    switch (severity) {
      case vscode.DiagnosticSeverity.Error:
        return "error";
      case vscode.DiagnosticSeverity.Warning:
        return "warning";
      case vscode.DiagnosticSeverity.Information:
        return "info";
      default:
        return "hint";
    }
  }

  private _parseWizardLaunchArgs(args?: unknown): { wizardId: string; goal: string; persona: string } | null {
    if (!args || typeof args !== "object") {
      return null;
    }
    const payload = args as Record<string, unknown>;
    return {
      wizardId: typeof payload.wizardId === "string" ? payload.wizardId : "bring_idea_to_life",
      goal: typeof payload.goal === "string" ? payload.goal : "",
      persona: typeof payload.persona === "string" ? payload.persona : "architect",
    };
  }

  private async _resolveWizardContext(ctx?: unknown): Promise<WizardDocContext | null> {
    if (ctx && typeof ctx === "object") {
      const payload = ctx as Record<string, unknown>;
      if (typeof payload.runId === "string" && typeof payload.stageId === "string" && typeof payload.wizardId === "string") {
        return {
          runId: payload.runId,
          stageId: payload.stageId,
          wizardId: payload.wizardId,
          title: typeof payload.title === "string" ? payload.title : payload.stageId,
        };
      }
    }

    const editor = vscode.window.activeTextEditor;
    if (editor && isWizardDoc(editor.document)) {
      return parseWizardDocContextFromDocument(editor.document);
    }

    const panelContext = this._wizardEditor.currentContext();
    if (panelContext) {
      return panelContext;
    }

    void vscode.window.showWarningMessage("Open a WaterFree wizard document first.");
    return null;
  }

  private async _saveWizardDocument(ctx: WizardDocContext): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      return;
    }
    const active = parseWizardDocContextFromDocument(editor.document);
    if (!active || active.runId !== ctx.runId || active.stageId !== ctx.stageId) {
      return;
    }
    if (editor.document.isDirty) {
      await editor.document.save();
    }
  }

  private _getWizardDraftContext(ctx: WizardDocContext): string {
    const draft = this._wizardEditor.currentDraftForContext(ctx);
    return draft?.trim() ?? "";
  }

  private async _handleWizardResponse(result: WizardResponse, focusDocument = true): Promise<void> {
    if (result.session && typeof result.session === "object") {
      const session = result.session as PlanData;
      this._sessionId = session.id;
      this._onPlanReceived(session);
    }

    if (result.wizard?.wizardId === "bring_idea_to_life") {
      await this._wizardEditor.showResponse(result);
      this._updateWizardEditorContext(vscode.window.activeTextEditor);
      return;
    }

    if (focusDocument && result.openDocPath) {
      await this._openWizardDocument(result.openDocPath);
    } else if (result.openDocPath && !focusDocument) {
      this._updateWizardEditorContext(vscode.window.activeTextEditor);
    }
  }

  private async _openWizardDocument(docPath: string): Promise<void> {
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(docPath));
    await vscode.window.showTextDocument(document, { preview: false, preserveFocus: false });
    this._updateWizardEditorContext(vscode.window.activeTextEditor);
  }

  private _updateWizardEditorContext(editor: vscode.TextEditor | undefined): void {
    const active = Boolean(
      this._wizardEditor.currentContext()
      || (editor && (isWizardDocPath(editor.document.uri.fsPath) || isWizardDoc(editor.document))),
    );
    void vscode.commands.executeCommand("setContext", "waterfree.wizardDocActive", active);
  }

  private async _onWizardEditorAction(action: WizardEditorAction): Promise<void> {
    this._sidebarProvider.setBusyMessage(action.type === "submit" ? "Running wizard stage..." : "Getting clarifying questions...");
    try {
      const result = await this._bridge.runWizardStep({
        runId: action.context.runId,
        stageId: action.context.stageId,
        mode: action.type === "refine" ? "clarify" : undefined,
        extraContext: action.body,
        workspacePath: this._workspacePath,
      });
      await this._handleWizardResponse(result, false);
    } catch (err) {
      this._handleError(action.type === "submit" ? "Run wizard stage failed" : "Refine idea failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  private async _runSnippetizeProcedure(name: string, focus: string): Promise<void> {
    this._sidebarProvider.setBusyMessage(`Snippetizing '${name}'...`);
    try {
      const result = await this._bridge.request<{
        entry: object | null;
        warnings: string[];
        tokenBudgetUsed: number;
        nodesIncluded: number;
        nodesSkipped: number;
        depthReached: number;
        kept: boolean;
        stored?: boolean;
      }>("extractProcedure", {
        name,
        workspacePath: this._workspacePath,
        focus,
        maxDepth: 3,
      });

      const warnings = result.warnings ?? [];

      if (!result.kept) {
        const reason = warnings.find((w) => w.startsWith("Symbol")) ?? "LLM judged it not worth storing.";
        void vscode.window.showWarningMessage(`WaterFree Snippetize: '${name}' not stored — ${reason}`);
        return;
      }

      let summary = `WaterFree Snippetize: '${name}' snippetized — `
        + `${result.nodesIncluded} call chain node(s) included`
        + (result.nodesSkipped > 0 ? `, ${result.nodesSkipped} skipped` : "")
        + ` (~${result.tokenBudgetUsed} tokens used).`;

      if (!result.stored) {
        summary += " (already in snippet store)";
      }

      const actions = warnings.length > 0 ? ["Show Warnings"] : [];
      const choice = await vscode.window.showInformationMessage(summary, ...actions);
      if (choice === "Show Warnings") {
        void vscode.window.showInformationMessage(warnings.join("\n"), { modal: true });
      }
    } catch (err) {
      this._handleError("Pair Snippetize failed", err);
    } finally {
      this._sidebarProvider.setBusyMessage(null);
    }
  }

  // ------------------------------------------------------------------
  // Session management
  // ------------------------------------------------------------------

  private async _autoIndex(): Promise<void> {
    // Retry with backoff — backend process may still be starting up when this fires.
    const delays = [500, 1500, 3000, 5000, 10000];
    for (const [attempt, delay] of delays.entries()) {
      try {
        await this._bridge.request("indexWorkspace", { path: this._workspacePath });
        this._log("index", `auto-index complete (attempt ${attempt + 1})`);
        return;
      } catch (err) {
        const isLastAttempt = attempt === delays.length - 1;
        if (isLastAttempt) {
          this._log("index", `auto-index failed after ${delays.length} attempts: ${err}`);
        } else {
          this._log("index", `auto-index attempt ${attempt + 1} failed, retrying in ${delay}ms`);
          await new Promise((r) => setTimeout(r, delay));
        }
      }
    }
  }

  private async _tryResumeSession(): Promise<void> {
    try {
      const session = await this._bridge.request<PlanData | null>("getSession", {
        workspacePath: this._workspacePath,
      });
      if (session) {
        this._sessionId = session.id;
        this._onPlanReceived(session);
        void vscode.window.showInformationMessage(
          `WaterFree: Resumed session — "${session.goalStatement}"`,
        );
      }
    } catch {
      // No prior session
    } finally {
      if (!this._sessionId) {
        this._sidebarProvider.setBacklogSummary(this._emptyBacklogSummary());
      }
      this._sidebarProvider.setCheckingForSession(false);
    }
  }

  private async _reloadSession(): Promise<void> {
    if (!this._sessionId) {
      return;
    }
    const session = await this._bridge.request<PlanData | null>("getSession", {
      sessionId: this._sessionId,
      workspacePath: this._workspacePath,
    });
    if (session) {
      this._onPlanReceived(session);
    }
  }

  private async _refreshBacklogSummary(): Promise<void> {
    if (!this._sessionId) {
      this._sidebarProvider.setBacklogSummary(this._emptyBacklogSummary());
      return;
    }

    try {
      const [nextResult, readyResult] = await Promise.all([
        this._bridge.request<{ task: BacklogSummaryData["nextTask"] }>("whatNext", {
          workspacePath: this._workspacePath,
        }),
        this._bridge.request<{ tasks: BacklogSummaryData["readyTasks"]; total?: number }>("listTasks", {
          workspacePath: this._workspacePath,
          readyOnly: true,
          limit: 5,
        }),
      ]);

      this._sidebarProvider.setBacklogSummary({
        nextTask: nextResult.task ?? null,
        readyTasks: readyResult.tasks ?? [],
        totalReady: readyResult.total ?? readyResult.tasks?.length ?? 0,
      });
    } catch {
      this._sidebarProvider.setBacklogSummary(this._emptyBacklogSummary());
    }
  }

  private _emptyBacklogSummary(): BacklogSummaryData {
    return {
      nextTask: null,
      readyTasks: [],
      totalReady: 0,
    };
  }

  private _onPlanReceived(plan: PlanData): void {
    this._plan = plan;
    this._sidebarProvider.setCheckingForSession(false);
    this._sidebarProvider.update(plan);
    void this._refreshBacklogSummary();
    this._refreshDecorations(plan);

    // Navigate to the first pending task's file
    const firstPending = plan.tasks.find(
      (t) => t.status === "pending" || t.status === "annotating" || t.status === "negotiating",
    );
    const targetFile = firstPending ? getTaskTargetPath(firstPending, this._workspacePath) : null;
    if (targetFile) {
      void vscode.window.showTextDocument(vscode.Uri.file(targetFile), {
        preview: true,
        preserveFocus: true,
      });
    }
  }

  private _onSessionUpdate(plan: PlanData): void {
    this._plan = plan;
    this._sidebarProvider.setCheckingForSession(false);
    this._sidebarProvider.update(plan);
    void this._refreshBacklogSummary();
    this._refreshDecorations(plan);
    if (plan.tasks.some((t) => t.status === "negotiating")) {
      this._statusBar.setState("awaiting_review");
    }
  }

  private _refreshDecorations(plan: PlanData): void {
    const pairs = plan.tasks.flatMap((task: TaskData) =>
      task.annotations.map((annotation) => ({ annotation, task })),
    );
    this._decorations.updateAnnotations(pairs, this._workspacePath);
  }

  private async _onSidebarAction(action: SidebarAction): Promise<void> {
    switch (action.type) {
      case "startSession":
        await this.cmdStart(action.goal, action.persona);
        return;
      case "openWizard": {
        await this.cmdOpenWizard({
          wizardId: action.wizardId,
          goal: action.goal,
          persona: action.persona,
        });
        return;
      }
      case "openTask":
        this._openTaskTarget(action.taskId);
        return;
      case "generateAnnotation":
        await this.cmdGenerateAnnotation(action.taskId);
        return;
      case "showAnnotation":
        this.cmdShowAnnotation(action.taskId, action.annotationId);
        return;
      case "approveAnnotation":
        await this.cmdApprove(action.annotationId);
        return;
      case "alterAnnotation":
        await this.cmdAlter(action.taskId, action.annotationId, action.feedback);
        return;
      case "redirectTask":
        await this.cmdRedirect(action.taskId, action.instruction);
        return;
      case "skipTask":
        await this.cmdSkipTask(action.taskId);
        return;
      case "buildKnowledge":
        await this.cmdBuildKnowledge();
        return;
      case "addKnowledgeRepo":
        await this.cmdAddKnowledgeRepo();
        return;
      case "snippetizeSymbol":
        await this._runSnippetizeProcedure(action.symbol, action.context);
        return;
      case "pushDebugToAgent":
        await this.cmdPushDebugToAgent(action.intent, action.stopReason);
        return;
    }
  }

  private _openTaskTarget(taskId: string): void {
    const task = this._plan?.tasks.find((candidate) => candidate.id === taskId);
    if (!task) {
      return;
    }
    const targetFile = getTaskTargetPath(task, this._workspacePath);
    if (!targetFile) {
      return;
    }
    const targetLine = getTaskTargetLine(task);
    void vscode.window.showTextDocument(vscode.Uri.file(targetFile), {
      selection: new vscode.Range(targetLine, 0, targetLine, 0),
      preview: true,
    });
  }

  private _updateDebugState(): void {
    const session = vscode.debug.activeDebugSession;
    const active = Boolean(session);
    const location = session?.name ?? "";
    this._sidebarProvider.setDebugState(active, active ? `Session: ${location}` : "");
  }

  private async _showDebugAnalysis(
    analysis: {
      diagnosis: string;
      likelyCause: string;
      suggestedFix: { summary: string; detail: string; targetFile?: string; targetLine?: number };
      questions?: string[];
    },
    _ctx: { file: string; line: number },
  ): Promise<void> {
    const qStr =
      analysis.questions?.length
        ? `\n\nQuestions before fixing:\n• ${analysis.questions.join("\n• ")}`
        : "";

    const detail =
      `Diagnosis: ${analysis.diagnosis}\n\n` +
      `Likely cause: ${analysis.likelyCause}\n\n` +
      `Suggested fix: ${analysis.suggestedFix.detail}` +
      qStr;

    const choice = await vscode.window.showInformationMessage(
      `[PP Debug] ${analysis.suggestedFix.summary}`,
      { detail, modal: true },
      "Add as Task",
      "Dismiss",
    );

    if (choice === "Add as Task") {
      try {
        const targetFile = analysis.suggestedFix.targetFile ?? _ctx.file;
        await this._bridge.request("queueTodoInstruction", {
          sessionId: this._sessionId ?? undefined,
          workspacePath: this._workspacePath,
          file: targetFile,
          line: analysis.suggestedFix.targetLine ?? _ctx.line,
          instruction: `Fix: ${analysis.suggestedFix.detail || analysis.suggestedFix.summary}`,
        });
        if (this._sessionId) {
          await this._reloadSession();
        }
        void vscode.window.showInformationMessage("WaterFree: Debug fix queued into the backlog.");
      } catch (err) {
        this._handleError("Could not add debug task", err);
      }
    }

    this._statusBar.setState("idle");
  }

  // ------------------------------------------------------------------
  // TODO watcher
  // ------------------------------------------------------------------

  private _onTodosFound(todos: WfTodo[]): void {
    // Queue each [wf] TODO into the durable backlog and attach it to the
    // active session when one exists.
    const queued = todos.map((todo) =>
      this._bridge
        .request("queueTodoInstruction", {
          sessionId: this._sessionId ?? undefined,
          workspacePath: this._workspacePath,
          file: todo.file,
          line: todo.line,
          instruction: todo.instruction,
        })
        .catch(() => undefined),
    );

    for (const todo of todos) {
      this._log(
        "todo",
        `queued: "${todo.instruction}" (${path.basename(todo.file)}:${todo.line})`,
      );
    }

    void Promise.allSettled(queued).then(() => this._refreshBacklogSummary());

    void vscode.window.showInformationMessage(
      `WaterFree: ${todos.length} [wf] TODO(s) queued into the backlog.`,
    );
  }

  // ------------------------------------------------------------------
  // Error handling
  // ------------------------------------------------------------------

  private _handleError(context: string, err: unknown): void {
    const message = err instanceof Error ? err.message : String(err);
    this._statusBar.setError(`${context}: ${message}`);
    this._log("error", `${context}: ${message}`);
    this._logger.show(true);
    void vscode.window.showErrorMessage(`WaterFree — ${context}: ${message}`);
  }

  private _log(scope: string, message: string): void {
    this._logger.log(scope, message);
  }

  // ------------------------------------------------------------------
  // Disposal
  // ------------------------------------------------------------------

  dispose(): void {
    this._disposables.forEach((d) => d.dispose());
  }
}
