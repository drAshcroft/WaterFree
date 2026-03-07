/**
 * LiveDebugCapture — queries the VS Code Debug Adapter Protocol to capture
 * the live program state when a developer right-clicks "Live Pair Debug".
 *
 * Uses a DebugAdapterTrackerFactory to intercept the DAP 'stopped' event
 * and record the active threadId. Variable queries are deferred until
 * the developer explicitly triggers the command.
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

// ------------------------------------------------------------------
// Tracker — intercepts DAP messages to capture stopped thread
// ------------------------------------------------------------------

class PairDebugTracker implements vscode.DebugAdapterTracker {
  constructor(private readonly _store: ThreadStore) {}

  onDidSendMessage(message: Record<string, unknown>): void {
    if (message["type"] === "event" && message["event"] === "stopped") {
      const body = message["body"] as Record<string, unknown> | undefined;
      const threadId = body?.["threadId"];
      if (typeof threadId === "number") {
        this._store.setThreadId(threadId);
      }
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

  constructor() {
    // Register for all debug types ('*')
    this._registration = vscode.debug.registerDebugAdapterTrackerFactory("*", this);
  }

  createDebugAdapterTracker(): vscode.DebugAdapterTracker {
    return new PairDebugTracker(this._store);
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

    const debugDir = vscode.Uri.file(path.join(workspacePath, ".waterfree", "debug"));
    await vscode.workspace.fs.createDirectory(debugDir);
    const snapshotUri = vscode.Uri.file(path.join(workspacePath, ".waterfree", "debug", "snapshot.json"));
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

  dispose(): void {
    this._registration.dispose();
  }
}
