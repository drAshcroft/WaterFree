/**
 * One ACP agent process and the JSON-RPC conversation with it.
 *
 * Transport mirrors PythonBridge: newline-delimited JSON on stdin/stdout, with
 * the agent's stderr kept strictly for logs (agents are required to keep stdout
 * clean for protocol traffic — Hermes documents this explicitly).
 *
 * Unlike PythonBridge this is bidirectional: the agent issues requests back to
 * us (`fs/*`, `session/request_permission`) which we answer via an
 * {@link AcpClientHost}. That inversion is the whole point — a subagent's file
 * writes arrive as requests we can gate rather than mutations we discover.
 */

import { ChildProcess, spawn } from "child_process";
import {
  ACP_PROTOCOL_VERSION,
  AcpMethod,
  AgentCapabilities,
  AuthMethod,
  ClientCapabilities,
  ContentBlock,
  InitializeResult,
  JSON_RPC_INTERNAL_ERROR,
  JSON_RPC_METHOD_NOT_FOUND,
  JsonRpcMessage,
  JsonRpcRequest,
  JsonRpcResponse,
  SessionModels,
  SessionNewResult,
  SessionPromptResult,
  SessionUpdateParams,
  isJsonRpcNotification,
  isJsonRpcRequest,
  isJsonRpcResponse,
} from "./AcpProtocol";

/** Everything the agent may ask of us during a session. */
export interface AcpClientHost {
  readTextFile(params: { sessionId: string; path: string; line?: number; limit?: number }): Promise<string>;
  writeTextFile(params: { sessionId: string; path: string; content: string }): Promise<void>;
  requestPermission(params: {
    sessionId: string;
    toolCall?: { toolCallId?: string; title?: string; kind?: string };
    options: Array<{ optionId: string; name?: string; kind?: string }>;
  }): Promise<{ optionId: string } | "cancelled">;
}

export interface AcpAgentSpec {
  /** Stable id used in config and logs, e.g. "hermes". */
  id: string;
  label: string;
  command: string;
  args: string[];
  env?: Record<string, string>;
  /** Absolute working directory for the agent process. */
  cwd: string;
}

/**
 * How an agent is described in configuration, before being resolved into a
 * spawnable {@link AcpAgentSpec}. Lives here rather than in the registry so the
 * driver can depend on it without pulling in `vscode`.
 */
export interface AcpAgentConfig {
  id: string;
  label: string;
  command: string;
  args: string[];
  env?: Record<string, string>;
  /** Pin a model for this agent's sessions (agent-specific id string). */
  model?: string;
  enabled?: boolean;
}

/** The subset of the agent catalog the driver needs; keeps it vscode-free. */
export interface AcpAgentCatalog {
  list(): AcpAgentConfig[];
  get(agentId: string): AcpAgentConfig | undefined;
  toSpec(config: AcpAgentConfig, cwd?: string): AcpAgentSpec;
}

export interface AcpConnectionOptions {
  spec: AcpAgentSpec;
  host: AcpClientHost;
  clientCapabilities?: ClientCapabilities;
  log?: (message: string) => void;
  onSessionUpdate?: (params: SessionUpdateParams) => void;
  /** Milliseconds before an outbound request is abandoned. */
  requestTimeoutMs?: number;
}

interface Pending {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
  method: string;
  timer: NodeJS.Timeout;
}

/**
 * Idle limit, not a turn limit. A healthy Hermes turn regularly runs past any
 * sane wall-clock bound (observed: >10 minutes on a single session/prompt while
 * streaming tool calls), so pending requests are failed only after the agent
 * has been *silent* this long — every inbound frame refreshes the clock.
 */
const DEFAULT_REQUEST_TIMEOUT_MS = 10 * 60 * 1000;

/** What we advertise unless a caller narrows it. */
const DEFAULT_CLIENT_CAPABILITIES: ClientCapabilities = {
  fs: { readTextFile: true, writeTextFile: true },
  // Terminal support is a separate method family we do not serve yet; claiming
  // it would strand the agent waiting on terminal/create.
  terminal: false,
};

export class AcpConnectionError extends Error {}

export class AcpConnection {
  private _proc: ChildProcess | null = null;
  private _buffer = "";
  private _nextId = 1;
  private readonly _pending = new Map<number, Pending>();
  private _disposed = false;
  private _initialized: InitializeResult | null = null;
  private _stderrTail: string[] = [];

  constructor(private readonly _options: AcpConnectionOptions) {}

  get spec(): AcpAgentSpec {
    return this._options.spec;
  }

  get agentCapabilities(): AgentCapabilities {
    return this._initialized?.agentCapabilities ?? {};
  }

  get authMethods(): AuthMethod[] {
    return this._initialized?.authMethods ?? [];
  }

  get isRunning(): boolean {
    return this._proc !== null && !this._proc.killed && this._proc.exitCode === null;
  }

  /** Last few stderr lines — the only diagnostic when an agent dies on startup. */
  get stderrTail(): string[] {
    return [...this._stderrTail];
  }

  // ── lifecycle ────────────────────────────────────────────────────────────

  /** Spawn the process and complete the initialize handshake. */
  async start(): Promise<InitializeResult> {
    if (this._proc) {
      throw new AcpConnectionError(`Agent '${this.spec.id}' is already started.`);
    }
    const { command, args, cwd, env } = this.spec;

    this._proc = spawn(command, args, {
      cwd,
      env: { ...process.env, ...(env ?? {}) },
      stdio: ["pipe", "pipe", "pipe"],
      // Own process group so a kill reaches the agent's children too. Agents
      // spawn tool subprocesses; killing only the parent leaks them.
      detached: process.platform !== "win32",
      windowsHide: true,
    });

    this._proc.stdout?.setEncoding("utf-8");
    this._proc.stdout?.on("data", (chunk: string) => this._onStdout(chunk));
    this._proc.stderr?.setEncoding("utf-8");
    this._proc.stderr?.on("data", (chunk: string) => this._onStderr(chunk));
    this._proc.on("error", (err) => this._failAll(new AcpConnectionError(
      `Failed to start agent '${this.spec.id}' (${command}): ${err.message}`,
    )));
    this._proc.on("exit", (code, signal) => {
      const detail = this._stderrTail.length ? ` stderr: ${this._stderrTail.slice(-3).join(" | ")}` : "";
      this._failAll(new AcpConnectionError(
        `Agent '${this.spec.id}' exited (code=${code}, signal=${signal}).${detail}`,
      ));
    });

    this._log(`spawned ${command} ${args.join(" ")} (pid=${this._proc.pid ?? "?"})`);

    const result = await this.request<InitializeResult>(AcpMethod.initialize, {
      protocolVersion: ACP_PROTOCOL_VERSION,
      clientCapabilities: this._options.clientCapabilities ?? DEFAULT_CLIENT_CAPABILITIES,
      clientInfo: { name: "waterfree", title: "WaterFree", version: "0.1.0" },
    });

    // A MAJOR mismatch means the shapes below may not apply. Surface it rather
    // than failing later with a confusing schema error.
    if (typeof result.protocolVersion === "number" && result.protocolVersion !== ACP_PROTOCOL_VERSION) {
      throw new AcpConnectionError(
        `Agent '${this.spec.id}' speaks ACP v${result.protocolVersion}; this client implements v${ACP_PROTOCOL_VERSION}.`,
      );
    }
    this._initialized = result;
    this._log(`initialized: ${result.agentInfo?.name ?? this.spec.id} ${result.agentInfo?.version ?? ""}`.trim());
    return result;
  }

  dispose(): void {
    if (this._disposed) {
      return;
    }
    this._disposed = true;
    this._failAll(new AcpConnectionError(`Agent '${this.spec.id}' connection disposed.`));
    const proc = this._proc;
    this._proc = null;
    if (!proc || proc.killed || proc.exitCode !== null) {
      return;
    }
    try {
      proc.stdin?.end();
    } catch {
      /* stdin may already be closed */
    }
    proc.kill();
    // An agent mid-tool-call can ignore SIGTERM; escalate rather than leak it.
    const pid = proc.pid;
    setTimeout(() => {
      if (proc.exitCode === null && !proc.killed) {
        try {
          proc.kill("SIGKILL");
        } catch {
          /* already gone */
        }
      }
      if (pid !== undefined) {
        this._log(`disposed (pid=${pid})`);
      }
    }, 2000).unref?.();
  }

  // ── session helpers ──────────────────────────────────────────────────────

  async newSession(cwd: string): Promise<{ sessionId: string; models?: SessionModels }> {
    const result = await this.request<SessionNewResult>(AcpMethod.sessionNew, { cwd, mcpServers: [] });
    const sessionId = result.sessionId ?? this._sessionIdFromMeta(result);
    if (!sessionId) {
      throw new AcpConnectionError(
        `Agent '${this.spec.id}' created a session but returned no sessionId.`,
      );
    }
    return { sessionId, models: result.models };
  }

  /**
   * Hermes returns the id under `_meta.hermes.sessionProvenance.acpSessionId`
   * rather than a top-level `sessionId`. Tolerated so the driver stays generic.
   */
  private _sessionIdFromMeta(result: SessionNewResult): string {
    const meta = result._meta as Record<string, any> | undefined;
    const provenance = meta?.hermes?.sessionProvenance;
    const id = provenance?.acpSessionId;
    return typeof id === "string" ? id : "";
  }

  async setModel(sessionId: string, modelId: string): Promise<void> {
    await this.request(AcpMethod.sessionSetModel, { sessionId, modelId });
  }

  async prompt(sessionId: string, prompt: ContentBlock[]): Promise<SessionPromptResult> {
    return this.request<SessionPromptResult>(AcpMethod.sessionPrompt, { sessionId, prompt });
  }

  /** Fire-and-forget: cancellation is a notification, so there is no ack. */
  cancel(sessionId: string): void {
    this.notify(AcpMethod.sessionCancel, { sessionId });
  }

  // ── transport ────────────────────────────────────────────────────────────

  request<T>(method: string, params: unknown): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (!this.isRunning) {
        reject(new AcpConnectionError(`Agent '${this.spec.id}' is not running.`));
        return;
      }
      const id = this._nextId++;
      const timeoutMs = this._options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new AcpConnectionError(
          `Agent '${this.spec.id}' timed out on ${method}: no agent activity for ${timeoutMs}ms.`,
        ));
      }, timeoutMs);
      timer.unref?.();
      this._pending.set(id, {
        resolve: resolve as (value: unknown) => void,
        reject,
        method,
        timer,
      });
      this._write({ jsonrpc: "2.0", id, method, params });
    });
  }

  notify(method: string, params: unknown): void {
    if (!this.isRunning) {
      return;
    }
    this._write({ jsonrpc: "2.0", method, params });
  }

  private _write(message: object): void {
    const line = `${JSON.stringify(message)}\n`;
    try {
      this._proc?.stdin?.write(line, "utf-8");
    } catch (err) {
      this._log(`write failed: ${(err as Error).message}`);
    }
  }

  private _onStdout(chunk: string): void {
    this._buffer += chunk;
    let index = this._buffer.indexOf("\n");
    while (index !== -1) {
      const line = this._buffer.slice(0, index).trim();
      this._buffer = this._buffer.slice(index + 1);
      if (line) {
        this._dispatchLine(line);
      }
      index = this._buffer.indexOf("\n");
    }
  }

  private _onStderr(chunk: string): void {
    for (const line of chunk.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) {
        continue;
      }
      this._stderrTail.push(trimmed);
      // Bounded: a chatty agent must not grow this without limit.
      if (this._stderrTail.length > 50) {
        this._stderrTail.shift();
      }
    }
  }

  private _dispatchLine(line: string): void {
    let message: JsonRpcMessage;
    try {
      message = JSON.parse(line) as JsonRpcMessage;
    } catch {
      // Agents are supposed to keep stdout protocol-only; log and carry on
      // rather than tearing down a working session over one stray line.
      this._log(`ignoring non-JSON stdout: ${line.slice(0, 200)}`);
      return;
    }

    // Any well-formed frame proves the agent is alive; give every pending
    // request its full idle window again.
    for (const pending of this._pending.values()) {
      pending.timer.refresh?.();
    }

    if (isJsonRpcResponse(message)) {
      this._settle(message);
      return;
    }
    if (isJsonRpcRequest(message)) {
      void this._serveRequest(message);
      return;
    }
    if (isJsonRpcNotification(message) && message.method === AcpMethod.sessionUpdate) {
      this._options.onSessionUpdate?.(message.params as SessionUpdateParams);
    }
  }

  private _settle(response: JsonRpcResponse): void {
    const id = typeof response.id === "number" ? response.id : Number(response.id);
    const pending = this._pending.get(id);
    if (!pending) {
      return;
    }
    this._pending.delete(id);
    clearTimeout(pending.timer);
    if (response.error) {
      pending.reject(new AcpConnectionError(
        `Agent '${this.spec.id}' failed ${pending.method}: ${response.error.message} (code ${response.error.code})`,
      ));
      return;
    }
    pending.resolve(response.result);
  }

  /** Serve an agent-initiated request against the host. */
  private async _serveRequest(request: JsonRpcRequest): Promise<void> {
    const { host } = this._options;
    const params = (request.params ?? {}) as any;
    // Log every agent-initiated request: when an agent stalls waiting on the
    // client, this line is the difference between a diagnosis and a mystery.
    this._log(`agent request ${request.method} (id=${request.id})${params.path ? ` path=${params.path}` : ""}`);
    try {
      let result: unknown;
      switch (request.method) {
        case AcpMethod.fsReadTextFile:
          result = { content: await host.readTextFile(params) };
          break;
        case AcpMethod.fsWriteTextFile:
          await host.writeTextFile(params);
          result = null;
          break;
        case AcpMethod.requestPermission: {
          const outcome = await host.requestPermission(params);
          result = outcome === "cancelled"
            ? { outcome: { outcome: "cancelled" } }
            : { outcome: { outcome: "selected", optionId: outcome.optionId } };
          break;
        }
        default:
          this._log(`refusing unsupported client method ${request.method}`);
          this._write({
            jsonrpc: "2.0",
            id: request.id,
            error: { code: JSON_RPC_METHOD_NOT_FOUND, message: `Unsupported client method: ${request.method}` },
          });
          return;
      }
      this._write({ jsonrpc: "2.0", id: request.id, result });
    } catch (err) {
      // Always answer. An unanswered request hangs the agent's turn forever.
      this._write({
        jsonrpc: "2.0",
        id: request.id,
        error: { code: JSON_RPC_INTERNAL_ERROR, message: (err as Error).message },
      });
    }
  }

  private _failAll(error: Error): void {
    for (const [, pending] of this._pending) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this._pending.clear();
  }

  private _log(message: string): void {
    this._options.log?.(`[acp:${this.spec.id}] ${message}`);
  }
}
