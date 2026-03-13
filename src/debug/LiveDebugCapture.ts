/**
 * LiveDebugCapture — queries the VS Code Debug Adapter Protocol to capture
 * the live program state when a developer right-clicks "Live Pair Debug".
 *
 * Uses a DebugAdapterTrackerFactory to intercept the DAP 'stopped' event
 * and record the active threadId. Variable queries are deferred until
 * the developer explicitly triggers the command.
 *
 * Also provides:
 *  - Auto-snapshot: writes live_state.json on every breakpoint hit
 *  - Eval watcher: watches for eval_request.json and executes expressions
 *    via DAP evaluate, enabling the MCP debug_eval tool
 */

import * as path from "path";
import * as vscode from "vscode";

export interface StackFrameData {
  name: string;
  file: string;
  line: number;
  column: number;
}

export interface DebugContextData {
  file: string;
  line: number;
  stackFrames: StackFrameData[];
  variables: Record<string, Record<string, string>>;
  exceptionMessage?: string;
}

export interface VarInfo {
  name: string;
  type: string;
  rawValue: string;
  variablesReference: number;
}

interface EvalRequest {
  requestId: string;
  expression: string;
  frameId?: number;
  timestamp: string;
}

interface EvalResponse {
  requestId: string;
  result?: string;
  resultType?: string;
  error?: string;
  timestamp: string;
}

// ------------------------------------------------------------------
// Tracker — intercepts DAP messages to capture stopped thread
// ------------------------------------------------------------------

class PairDebugTracker implements vscode.DebugAdapterTracker {
  constructor(
    private readonly _store: ThreadStore,
    private readonly _onStopped?: () => void,
  ) {}

  onDidSendMessage(message: Record<string, unknown>): void {
    if (message["type"] === "event" && message["event"] === "stopped") {
      const body = message["body"] as Record<string, unknown> | undefined;
      const threadId = body?.["threadId"];
      if (typeof threadId === "number") {
        this._store.setThreadId(threadId);
      }
      this._onStopped?.();
    }
  }
}

class ThreadStore {
  private _threadId: number | null = null;

  setThreadId(id: number): void {
    this._threadId = id;
  }

  getThreadId(): number | null {
    return this._threadId;
  }
}

// ------------------------------------------------------------------
// Factory — one tracker per debug session
// ------------------------------------------------------------------

export class LiveDebugCapture implements vscode.DebugAdapterTrackerFactory, vscode.Disposable {
  private readonly _store = new ThreadStore();
  private readonly _registration: vscode.Disposable;
  private _workspacePath: string | null = null;
  private _evalWatcher: vscode.FileSystemWatcher | null = null;
  private _topFrameId: number | null = null;
  private _breakpointHitCallbacks: Array<(location: { file: string; line: number; qualifiedName: string; exceptionMessage?: string }) => void> = [];

  constructor() {
    // Register for all debug types ('*')
    this._registration = vscode.debug.registerDebugAdapterTrackerFactory("*", this);
  }

  createDebugAdapterTracker(): vscode.DebugAdapterTracker {
    return new PairDebugTracker(this._store, () => void this._handleBreakpointHit());
  }

  // ------------------------------------------------------------------
  // Workspace / lifecycle
  // ------------------------------------------------------------------

  setWorkspacePath(workspacePath: string): void {
    this._workspacePath = workspacePath;
  }

  onBreakpointHit(
    callback: (location: { file: string; line: number; qualifiedName: string; exceptionMessage?: string }) => void,
  ): vscode.Disposable {
    this._breakpointHitCallbacks.push(callback);
    return new vscode.Disposable(() => {
      this._breakpointHitCallbacks = this._breakpointHitCallbacks.filter((cb) => cb !== callback);
    });
  }

  // ------------------------------------------------------------------
  // Auto-snapshot on breakpoint
  // ------------------------------------------------------------------

  private async _handleBreakpointHit(): Promise<void> {
    if (!this._workspacePath) {
      return;
    }
    const session = vscode.debug.activeDebugSession;
    if (!session) {
      return;
    }

    try {
      const frames = await this._getStackFrames(session);
      this._topFrameId = (frames[0] as (StackFrameData & { id?: number }))?.id ?? null;

      const exceptionMessage = await this._getExceptionMessage(session);
      const location = {
        file: frames[0]?.file ?? "",
        line: frames[0]?.line ?? 0,
        qualifiedName: frames[0]?.name ?? "",
        exceptionMessage,
      };

      // Write lightweight live_state.json so debug_status shows active immediately
      const liveState = {
        capturedAt: new Date().toISOString(),
        stopReason: exceptionMessage ? "exception" : "breakpoint",
        location: {
          file: location.file,
          line: location.line,
          qualifiedName: location.qualifiedName,
        },
        exceptionMessage: exceptionMessage ?? null,
      };
      await this._writeDebugFile("live_state.json", JSON.stringify(liveState, null, 2));

      for (const cb of this._breakpointHitCallbacks) {
        cb(location);
      }
    } catch {
      // Don't disrupt the debug session on errors
    }
  }

  clearLiveState(): void {
    if (!this._workspacePath) {
      return;
    }
    this._topFrameId = null;
    const liveStatePath = vscode.Uri.file(
      path.join(this._workspacePath, ".waterfree", "debug", "live_state.json"),
    );
    void vscode.workspace.fs.delete(liveStatePath, { useTrash: false }).then(
      () => {},
      () => {},
    );
  }

  // ------------------------------------------------------------------
  // Eval watcher — file-based RPC for debug_eval MCP tool
  // ------------------------------------------------------------------

  startEvalWatcher(): void {
    if (!this._workspacePath || this._evalWatcher) {
      return;
    }
    const debugDir = path.join(this._workspacePath, ".waterfree", "debug");
    const pattern = new vscode.RelativePattern(debugDir, "eval_request.json");
    this._evalWatcher = vscode.workspace.createFileSystemWatcher(pattern);
    this._evalWatcher.onDidCreate(() => void this._handleEvalRequest());
    this._evalWatcher.onDidChange(() => void this._handleEvalRequest());
  }

  stopEvalWatcher(): void {
    this._evalWatcher?.dispose();
    this._evalWatcher = null;
  }

  private async _handleEvalRequest(): Promise<void> {
    if (!this._workspacePath) {
      return;
    }
    const session = vscode.debug.activeDebugSession;
    if (!session) {
      return;
    }

    const requestPath = vscode.Uri.file(
      path.join(this._workspacePath, ".waterfree", "debug", "eval_request.json"),
    );

    let req: EvalRequest;
    try {
      const raw = await vscode.workspace.fs.readFile(requestPath);
      req = JSON.parse(Buffer.from(raw).toString("utf8")) as EvalRequest;
    } catch {
      return;
    }

    // Use stored top frame id if not specified
    const frameId = req.frameId ?? this._topFrameId ?? 0;

    let response: EvalResponse;
    try {
      const evalResp = await session.customRequest("evaluate", {
        expression: req.expression,
        frameId,
        context: "repl",
      }) as { result: string; type?: string };
      response = {
        requestId: req.requestId,
        result: evalResp.result,
        resultType: evalResp.type,
        timestamp: new Date().toISOString(),
      };
    } catch (err) {
      response = {
        requestId: req.requestId,
        error: err instanceof Error ? err.message : String(err),
        timestamp: new Date().toISOString(),
      };
    }

    await this._writeDebugFile("eval_response.json", JSON.stringify(response, null, 2));
  }

  // ------------------------------------------------------------------
  // Public: capture current debug state
  // ------------------------------------------------------------------

  async capture(): Promise<DebugContextData | null> {
    const session = vscode.debug.activeDebugSession;
    if (!session) {
      return null;
    }

    const editor = vscode.window.activeTextEditor;
    const currentFile = editor?.document.uri.fsPath ?? "";
    const currentLine = (editor?.selection.active.line ?? 0) + 1;

    // 1. Get stack frames
    const stackFrames = await this._getStackFrames(session);

    // 2. Get variables from the top frame
    const variables = await this._getVariables(session, stackFrames);

    // 3. Check for an active exception in the stopped event's description
    const exceptionMessage = await this._getExceptionMessage(session);

    return {
      file: stackFrames[0]?.file ?? currentFile,
      line: stackFrames[0]?.line ?? currentLine,
      stackFrames,
      variables,
      exceptionMessage,
    };
  }

  // ------------------------------------------------------------------
  // DAP queries
  // ------------------------------------------------------------------

  private async _getStackFrames(
    session: vscode.DebugSession,
  ): Promise<StackFrameData[]> {
    const threadId = this._store.getThreadId();
    if (threadId === null) {
      // Fall back: try to get threads and pick the first
      try {
        const threads = await session.customRequest("threads", {}) as {
          threads: Array<{ id: number; name: string }>;
        };
        if (!threads.threads?.length) {
          return [];
        }
        this._store.setThreadId(threads.threads[0].id);
      } catch {
        return [];
      }
    }

    try {
      const response = await session.customRequest("stackTrace", {
        threadId: this._store.getThreadId(),
        startFrame: 0,
        levels: 20,
      }) as { stackFrames: Array<{
        id: number;
        name: string;
        source?: { path?: string; name?: string };
        line: number;
        column: number;
      }> };

      return (response.stackFrames ?? []).map((f) => ({
        id: f.id,
        name: f.name,
        file: f.source?.path ?? f.source?.name ?? "",
        line: f.line,
        column: f.column,
      }));
    } catch {
      return [];
    }
  }

  private async _getVariables(
    session: vscode.DebugSession,
    frames: StackFrameData[],
  ): Promise<Record<string, Record<string, string>>> {
    const topFrame = frames[0] as (StackFrameData & { id?: number }) | undefined;
    if (!topFrame?.id) {
      return {};
    }

    try {
      const scopesResp = await session.customRequest("scopes", {
        frameId: topFrame.id,
      }) as { scopes: Array<{ name: string; variablesReference: number; expensive?: boolean }> };

      const result: Record<string, Record<string, string>> = {};

      for (const scope of scopesResp.scopes ?? []) {
        if (scope.expensive) {
          continue; // skip e.g. "Globals" in large Python programs
        }
        const vars = await this._fetchVariables(session, scope.variablesReference);
        if (Object.keys(vars).length > 0) {
          result[scope.name] = vars;
        }
      }

      return result;
    } catch {
      return {};
    }
  }

  private async _fetchVariables(
    session: vscode.DebugSession,
    reference: number,
    depth = 0,
  ): Promise<Record<string, string>> {
    if (depth > 1 || reference === 0) {
      return {};
    }
    try {
      const resp = await session.customRequest("variables", {
        variablesReference: reference,
        count: 100,
      }) as { variables: Array<{ name: string; value: string; variablesReference: number }> };

      const result: Record<string, string> = {};
      for (const v of resp.variables ?? []) {
        // Truncate long values
        result[v.name] = v.value.length > 300 ? v.value.slice(0, 300) + "…" : v.value;
      }
      return result;
    } catch {
      return {};
    }
  }

  private async _getExceptionMessage(
    session: vscode.DebugSession,
  ): Promise<string | undefined> {
    // Some adapters support exceptionInfo request
    const threadId = this._store.getThreadId();
    if (threadId === null) {
      return undefined;
    }
    try {
      const resp = await session.customRequest("exceptionInfo", {
        threadId,
      }) as { exceptionId?: string; description?: string; details?: { message?: string } };

      const parts = [resp.exceptionId, resp.description ?? resp.details?.message].filter(Boolean);
      return parts.length > 0 ? parts.join(": ") : undefined;
    } catch {
      return undefined; // adapter doesn't support exceptionInfo — that's fine
    }
  }

  // ------------------------------------------------------------------
  // Snapshot — write debug state to disk for MCP agent access
  // ------------------------------------------------------------------

  async writeSnapshot(intent: string, stopReason: string, workspacePath: string): Promise<string> {
    const ctx = await this.capture();
    if (!ctx) {
      throw new Error("No active debug session or could not capture state.");
    }

    const session = vscode.debug.activeDebugSession;
    const richScopes = session
      ? await this._getRichScopes(session, ctx.stackFrames as (StackFrameData & { id?: number })[])
      : {};

    const snapshot = {
      capturedAt: new Date().toISOString(),
      intent,
      stopReason,
      location: {
        file: ctx.file,
        line: ctx.line,
        qualifiedName: ctx.stackFrames[0]?.name ?? "",
      },
      callStack: ctx.stackFrames.map((f) => ({ frame: f.name, file: f.file, line: f.line })),
      scopes: richScopes,
      exceptionMessage: ctx.exceptionMessage ?? null,
    };

    const snapshotUri = vscode.Uri.file(path.join(workspacePath, ".waterfree", "debug", "snapshot.json"));
    await vscode.workspace.fs.createDirectory(vscode.Uri.file(path.dirname(snapshotUri.fsPath)));
    await vscode.workspace.fs.writeFile(snapshotUri, Buffer.from(JSON.stringify(snapshot, null, 2), "utf8"));
    return snapshotUri.fsPath;
  }

  private async _getRichScopes(
    session: vscode.DebugSession,
    frames: (StackFrameData & { id?: number })[],
  ): Promise<Record<string, VarInfo[]>> {
    const topFrame = frames[0];
    if (!topFrame?.id) {
      return {};
    }
    try {
      const scopesResp = await session.customRequest("scopes", { frameId: topFrame.id }) as {
        scopes: Array<{ name: string; variablesReference: number; expensive?: boolean }>;
      };
      const result: Record<string, VarInfo[]> = {};
      for (const scope of scopesResp.scopes ?? []) {
        if (scope.expensive) {
          continue;
        }
        const vars = await this._fetchRichVariables(session, scope.variablesReference);
        if (vars.length > 0) {
          result[scope.name] = vars;
        }
      }
      return result;
    } catch {
      return {};
    }
  }

  private async _fetchRichVariables(session: vscode.DebugSession, reference: number): Promise<VarInfo[]> {
    if (reference === 0) {
      return [];
    }
    try {
      const resp = await session.customRequest("variables", {
        variablesReference: reference,
        count: 200,
      }) as { variables: Array<{ name: string; value: string; type?: string; variablesReference: number }> };
      return (resp.variables ?? []).slice(0, 200).map((v) => ({
        name: v.name,
        type: v.type ?? "",
        rawValue: v.value.length > 500 ? v.value.slice(0, 500) + "\u2026" : v.value,
        variablesReference: v.variablesReference,
      }));
    } catch {
      return [];
    }
  }

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------

  private async _writeDebugFile(filename: string, content: string): Promise<void> {
    if (!this._workspacePath) {
      return;
    }
    const debugDir = vscode.Uri.file(path.join(this._workspacePath, ".waterfree", "debug"));
    await vscode.workspace.fs.createDirectory(debugDir);
    const fileUri = vscode.Uri.file(path.join(this._workspacePath, ".waterfree", "debug", filename));
    await vscode.workspace.fs.writeFile(fileUri, Buffer.from(content, "utf8"));
  }

  dispose(): void {
    this._registration.dispose();
    this.stopEvalWatcher();
  }
}
