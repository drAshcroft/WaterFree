/**
 * PythonBridge — stdin/stdout JSON-RPC client.
 *
 * Spawns the Python backend process and provides a typed async interface
 * for sending requests and receiving notifications.
 *
 * Protocol: newline-delimited JSON on stdin/stdout.
 *   → {"id":"1","method":"indexWorkspace","params":{...}}
 *   ← {"id":"1","result":{...}}           (response)
 *   ← {"method":"indexProgress","params":{...}}  (notification — no id)
 */

import { ChildProcess, spawn, spawnSync } from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

import { PairLoggerLike } from "../logging/PairLogger.js";

export type NotificationHandler = (params: unknown) => void;

export type RuntimeInfo = {
  id: string;
  label: string;
  provider: "anthropic" | "ollama" | "openai" | "deep_agents" | "custom";
  local: boolean;
  supportsTools: boolean;
  supportsSkills: boolean;
  supportsCheckpoints: boolean;
};

export type SkillInfo = {
  id: string;
  title: string;
  description: string;
  path: string;
  appliesTo: string[];
  hasScripts: boolean;
  hasReferences: boolean;
};

export type CheckpointInfo = {
  id: string;
  sessionId: string;
  reason: string;
  createdAt: string;
  runtimeId: string;
  subagentId?: string;
  requiresApproval: boolean;
  summary: string;
  touchedFiles: string[];
  toolCalls: Array<{ serverId: string; toolName: string }>;
};

export type SubagentInfo = {
  id: string;
  label: string;
  skills: string[];
};

export type BackendProviderProfile = {
  version: number;
  activeProviderId: string;
  catalog: Array<{ id?: string; type?: string }>;
  policies: unknown;
};

export type WizardChunkData = {
  id: string;
  title: string;
  required: boolean;
  guidance?: string;
  notesSnapshot?: string;
  draftText?: string;
  acceptedText?: string;
  status: "draft" | "accepted";
  updatedAt?: string;
};

export type WizardTodoExport = {
  id: string;
  stageId: string;
  title: string;
  description: string;
  prompt: string;
  phase: string;
  priority: "P0" | "P1" | "P2" | "P3" | "spike";
  taskType: "impl" | "test" | "spike" | "review" | "refactor";
  targetCoord: {
    file: string;
    class?: string;
    method?: string;
    line?: number;
    anchorType: "create-at" | "modify" | "delete" | "read-only-context";
  };
  ownerType: "human" | "agent" | "unassigned";
  ownerName: string;
  promotedTaskId?: string | null;
};

export type WizardStageData = {
  id: string;
  kind: string;
  title: string;
  persona: string;
  docPath: string;
  status: "pending" | "drafted" | "accepted";
  subsystemName?: string;
  chunks: WizardChunkData[];
  todoExports: WizardTodoExport[];
  summary?: string;
  questions?: string[];
  externalResearchPrompt?: string;
  derivedArtifacts?: Record<string, unknown>;
  updatedAt?: string;
};

export type WizardRunData = {
  id: string;
  wizardId: string;
  goal: string;
  persona: string;
  workspacePath: string;
  status: "active" | "coding" | "complete";
  currentStageId: string;
  stages: WizardStageData[];
  derivedTaskIds: Record<string, string>;
  linkedSessionId?: string | null;
  createdAt: string;
  updatedAt: string;
};

export type WizardResponse = {
  wizard: WizardRunData | null;
  openDocPath: string;
  stageId?: string;
  chunkId?: string;
  createdTaskIds?: string[];
  count?: number;
  session?: object;
};

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
}

export class PythonBridge implements vscode.Disposable {
  private _proc: ChildProcess | null = null;
  private _buffer = "";
  private _pending = new Map<string, PendingRequest>();
  private _notificationHandlers = new Map<string, NotificationHandler[]>();
  private _nextId = 1;
  private _disposed = false;
  private _anthropicApiKey = "";
  private _providerProfile: BackendProviderProfile | null = null;
  private readonly _logger: PairLoggerLike;
  private readonly _backendLogFilePath: string;

  constructor(
    private readonly _workspacePath: string,
    private readonly _extensionPath: string,
    logger: PairLoggerLike,
  ) {
    this._logger = logger;
    this._backendLogFilePath = path.join(
      this._workspacePath,
      ".waterfree",
      "logs",
      "backend.log",
    );
  }

  // ------------------------------------------------------------------
  // Lifecycle
  // ------------------------------------------------------------------

  start(): void {
    if (this._proc && !this._proc.killed && this._proc.exitCode === null) {
      return;
    }
    this._proc = null;

    const config = vscode.workspace.getConfiguration("waterfree");
    const apiKey = this._anthropicApiKey || process.env.ANTHROPIC_API_KEY || "";
    const graphBinary: string = config.get<string>("graphBinaryPath") || "codebase-memory-mcp";
    const webSearchProvider: string = config.get<string>("webSearch.provider") ?? "none";
    const webSearchApiKey: string = config.get<string>("webSearch.apiKey") ?? "";

    const activeProviderType = this._resolveActiveProviderType();
    if (apiKey) {
      const masked = `${apiKey.slice(0, 7)}…${apiKey.slice(-4)}`;
      this._log(`Anthropic API key found (${masked}) — will pass to backend`);
    } else if (!activeProviderType || activeProviderType === "claude" || activeProviderType === "anthropic") {
      this._log("WARNING: No Anthropic API key configured. Run WaterFree: Setup or set ANTHROPIC_API_KEY.");
      vscode.window.showWarningMessage(
        "WaterFree: No Anthropic API key found. Run WaterFree: Setup or set ANTHROPIC_API_KEY.",
      );
    }

    const webSearchEnabled = webSearchProvider && webSearchProvider !== "none";
    const resolvedWebKey = webSearchApiKey || process.env.WATERFREE_WEB_SEARCH_API_KEY || "";
    const env = {
      ...process.env,
      WATERFREE_BACKEND_LOG_FILE: this._backendLogFilePath,
      WATERFREE_EXTENSION_LOG_FILE: this._logger.logFilePath,
      WATERFREE_GRAPH_BINARY: graphBinary,
      ...(apiKey ? { ANTHROPIC_API_KEY: apiKey } : {}),
      ...(webSearchEnabled ? {
        WATERFREE_ENABLE_WEB_TOOLS: "1",
        WATERFREE_WEB_SEARCH_PROVIDER: webSearchProvider,
        ...(resolvedWebKey ? { WATERFREE_WEB_SEARCH_API_KEY: resolvedWebKey } : {}),
      } : {}),
    };

    // Resolve the backend command.
    // Production: use the self-contained exe bundled with the extension.
    // Dev fallback: use waterfree.pythonPath setting (or "python") with -m backend.server.
    const arch = process.arch === "x64" ? "x64" : process.arch;
    const exeName = process.platform === "win32"
      ? `waterfree-win32-${arch}.exe`
      : `waterfree-${process.platform}-${arch}`;
    const bundledExe = path.join(this._extensionPath, "bin", exeName);

    let cmd: string;
    let args: string[];
    if (fs.existsSync(bundledExe)) {
      cmd = bundledExe;
      args = ["serve"];
    } else {
      const pythonPath: string = config.get("pythonPath") ?? "python";
      cmd = pythonPath;
      args = ["-m", "backend.server"];
    }

    fs.mkdirSync(path.dirname(this._backendLogFilePath), { recursive: true });
    this._log(
      `Starting backend: ${cmd} ${args.join(" ")} ` +
      `(cwd=${this._extensionPath}, backendLog=${this._backendLogFilePath})`,
    );

    this._proc = spawn(cmd, args, {
      cwd: this._extensionPath,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this._log(`Python backend spawned${this._proc.pid ? ` (pid=${this._proc.pid})` : ""}`);

    this._proc.stdout?.setEncoding("utf-8");
    this._proc.stdout?.on("data", (chunk: string) => this._onData(chunk));

    this._proc.stderr?.setEncoding("utf-8");
    this._proc.stderr?.on("data", (chunk: string) => {
      // Log Python stderr to the output channel for debugging
      for (const line of chunk.split("\n").filter(Boolean)) {
        this._log(line, "py");
      }
    });

    this._proc.on("exit", (code, signal) => {
      this._log(`Python backend exited (code=${code}, signal=${signal})`);
      this._proc = null;
      // Reject all pending requests
      for (const [, req] of this._pending) {
        req.reject(new Error(`Backend exited unexpectedly (code=${code})`));
      }
      this._pending.clear();

      if (!this._disposed) {
        this._logger.show(true);
        vscode.window.showWarningMessage(
          "WaterFree backend stopped. Check WaterFree output or " +
          `${this._backendLogFilePath}, then restart VS Code or re-run WaterFree: Start Session.`,
        );
      }
    });

    this._proc.on("error", (err) => {
      this._log(`Failed to start Python backend: ${err.message}`);
      this._proc = null;
      this._logger.show(true);
      vscode.window.showErrorMessage(
        `WaterFree: Could not start Python backend — ${err.message}. Check waterfree.pythonPath.`,
      );
    });

    if (this._providerProfile) {
      void this.syncProviderProfile(this._providerProfile).catch((err: Error) => {
        this._log(`syncProviderProfile failed: ${err.message}`);
      });
    }
  }

  dispose(): void {
    this._disposed = true;
    const proc = this._proc;
    this._proc = null;
    this._rejectPending(new Error("WaterFree backend stopped."));
    if (proc) {
      this._terminateBackend(proc);
    }
  }

  get isRunning(): boolean {
    return this._proc !== null && !this._proc.killed;
  }

  setAnthropicApiKey(apiKey: string): void {
    this._anthropicApiKey = apiKey.trim();
  }

  setProviderProfile(profile: BackendProviderProfile): void {
    this._providerProfile = profile;
  }

  restart(): void {
    const proc = this._proc;
    this._proc = null;
    this._rejectPending(new Error("WaterFree backend restarting."));
    if (proc) {
      this._terminateBackend(proc);
    }
    this.start();
  }

  // ------------------------------------------------------------------
  // Request / notification API
  // ------------------------------------------------------------------

  request<T = unknown>(method: string, params: object = {}): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (!this._proc || !this._proc.stdin || this._proc.killed || this._proc.exitCode !== null) {
        this._log("Backend is not running; attempting automatic restart.");
        this.start();
      }

      if (!this._proc || !this._proc.stdin || this._proc.killed || this._proc.exitCode !== null) {
        reject(
          new Error(
            "WaterFree backend is not running. Check waterfree.pythonPath and backend logs.",
          ),
        );
        return;
      }

      const id = String(this._nextId++);
      this._log(`request ${id} -> ${method}`);
      this._pending.set(id, {
        resolve: resolve as (v: unknown) => void,
        reject,
      });

      const msg = JSON.stringify({ id, method, params }) + "\n";
      this._proc.stdin.write(msg, "utf-8", (err) => {
        if (err) {
          this._pending.delete(id);
          this._log(`request ${id} write failed: ${err.message}`);
          reject(err);
        }
      });
    });
  }

  onNotification(method: string, handler: NotificationHandler): vscode.Disposable {
    const list = this._notificationHandlers.get(method) ?? [];
    list.push(handler);
    this._notificationHandlers.set(method, list);
    return new vscode.Disposable(() => {
      const updated = (this._notificationHandlers.get(method) ?? []).filter(
        (h) => h !== handler,
      );
      this._notificationHandlers.set(method, updated);
    });
  }

  listRuntimes(): Promise<{ runtimes: RuntimeInfo[] }> {
    return this.request("listRuntimes", {});
  }

  getActiveRuntime(): Promise<{ runtimeId: string }> {
    return this.request("getActiveRuntime", {});
  }

  setActiveRuntime(runtimeId: string): Promise<{ ok: boolean; runtimeId: string }> {
    return this.request("setActiveRuntime", { runtimeId });
  }

  syncProviderProfile(profile: BackendProviderProfile): Promise<{ ok: boolean; profileHash: string }> {
    this._providerProfile = profile;
    return this.request("syncProviderProfile", { profile, workspacePath: this._workspacePath });
  }

  listSkills(params: { persona?: string; stage?: string } = {}): Promise<{ skills: SkillInfo[] }> {
    return this.request("listSkills", params);
  }

  reloadSkills(): Promise<{ ok: boolean; count: number }> {
    return this.request("reloadSkills", {});
  }

  getSkillDetail(skillId: string): Promise<{ markdown: string; references: string[]; scripts: string[] }> {
    return this.request("getSkillDetail", { skillId });
  }

  listCheckpoints(
    params: { sessionId?: string; workspacePath?: string } = {},
  ): Promise<{ checkpoints: CheckpointInfo[] }> {
    return this.request("listCheckpoints", params);
  }

  resumeCheckpoint(
    checkpointId: string,
    decision: Record<string, unknown>,
  ): Promise<{ ok: boolean; checkpoint: CheckpointInfo }> {
    return this.request("resumeCheckpoint", { checkpointId, decision });
  }

  discardCheckpoint(checkpointId: string): Promise<{ ok: boolean; checkpointId: string }> {
    return this.request("discardCheckpoint", { checkpointId });
  }

  listSubagents(): Promise<{ subagents: SubagentInfo[] }> {
    return this.request("listSubagents", {});
  }

  delegateToSubagent(params: {
    sessionId: string;
    subagentId: string;
    taskId: string;
    prompt: string;
    workspacePath?: string;
  }): Promise<{ checkpointId?: string; result?: object | null }> {
    return this.request("delegateToSubagent", params);
  }

  createWizardSession(params: {
    goal: string;
    wizardId: string;
    publicDocsPath?: string;
    workspacePath?: string;
    persona?: string;
  }): Promise<WizardResponse> {
    return this.request("createWizardSession", params);
  }

  getWizardSession(params: { runId?: string; workspacePath?: string } = {}): Promise<WizardResponse> {
    return this.request("getWizardSession", params);
  }

  runWizardStep(params: {
    runId: string;
    stageId: string;
    chunkId?: string;
    revisionNote?: string;
    extraContext?: string;
    mode?: "clarify" | "research";
    workspacePath?: string;
  }): Promise<WizardResponse> {
    return this.request("runWizardStep", params);
  }

  acceptWizardChunk(params: {
    runId: string;
    stageId: string;
    chunkId: string;
    workspacePath?: string;
  }): Promise<WizardResponse> {
    return this.request("acceptWizardChunk", params);
  }

  acceptWizardStep(params: {
    runId: string;
    stageId: string;
    workspacePath?: string;
  }): Promise<WizardResponse> {
    return this.request("acceptWizardStep", params);
  }

  promoteWizardTodos(params: {
    runId: string;
    workspacePath?: string;
  }): Promise<WizardResponse> {
    return this.request("promoteWizardTodos", params);
  }

  startWizardCoding(params: {
    runId: string;
    workspacePath?: string;
  }): Promise<WizardResponse> {
    return this.request("startWizardCoding", params);
  }

  runWizardReview(params: {
    runId: string;
    workspacePath?: string;
  }): Promise<WizardResponse> {
    return this.request("runWizardReview", params);
  }

  // ------------------------------------------------------------------
  // Internal
  // ------------------------------------------------------------------

  private _onData(chunk: string): void {
    this._buffer += chunk;
    const lines = this._buffer.split("\n");
    // Keep the incomplete tail in the buffer
    this._buffer = lines.pop() ?? "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        continue;
      }
      this._dispatch(trimmed);
    }
  }

  private _dispatch(json: string): void {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(json) as Record<string, unknown>;
    } catch {
      this._log(`Could not parse backend message: ${json}`);
      return;
    }

    // Notification — no id field
    if (!("id" in msg) && typeof msg.method === "string") {
      this._log(`notification <- ${msg.method}`);
      const handlers = this._notificationHandlers.get(msg.method) ?? [];
      for (const h of handlers) {
        h(msg.params);
      }
      return;
    }

    // Response — has id field
    const id = String(msg.id);
    const pending = this._pending.get(id);
    if (!pending) {
      this._log(`Received response for unknown request id=${id}`);
      return;
    }
    this._pending.delete(id);

    if ("error" in msg && msg.error) {
      const err = msg.error as { message?: string };
      this._log(`request ${id} failed: ${err.message ?? "Unknown backend error"}`);
      pending.reject(new Error(err.message ?? "Unknown backend error"));
    } else {
      this._log(`request ${id} <- ok`);
      pending.resolve(msg.result);
    }
  }

  private _log(text: string, scope: "bridge" | "py" = "bridge"): void {
    this._logger.log(scope, text);
  }

  private _rejectPending(err: Error): void {
    for (const [, req] of this._pending) {
      req.reject(err);
    }
    this._pending.clear();
  }

  private _terminateBackend(proc: ChildProcess): void {
    const pid = proc.pid;

    try {
      proc.stdin?.end();
    } catch {
      // Ignore stream shutdown errors during teardown.
    }

    try {
      proc.kill();
    } catch {
      // Ignore kill errors and proceed to force-terminate fallbacks.
    }

    if (!pid) {
      return;
    }

    if (process.platform === "win32") {
      const result = spawnSync(
        "taskkill",
        ["/PID", String(pid), "/T", "/F"],
        { windowsHide: true, stdio: "ignore" },
      );
      if (result.error) {
        this._log(`taskkill failed for PID ${pid}: ${result.error.message}`);
      }
      return;
    }

    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // Process already exited.
    }
  }

  private _resolveActiveProviderType(): string {
    if (!this._providerProfile) {
      return "";
    }
    const activeId = String(this._providerProfile.activeProviderId ?? "");
    const active = this._providerProfile.catalog.find((entry) => String(entry.id ?? "") === activeId)
      ?? this._providerProfile.catalog[0];
    return String(active?.type ?? "");
  }
}
