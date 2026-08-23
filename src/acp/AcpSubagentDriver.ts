/**
 * Drives ACP agent processes as WaterFree subagents.
 *
 * One delegation == one agent process == one ACP session. Processes are not
 * pooled across delegations: a subagent's value is an isolated context, and
 * reuse would leak one task's history into the next. The supervisor exists to
 * guarantee every spawned process is eventually reaped.
 */

import {
  AcpAgentCatalog,
  AcpAgentConfig,
  AcpAgentSpec,
  AcpClientHost,
  AcpConnection,
  AcpConnectionError,
} from "./AcpConnection";
import {
  ContentBlock,
  ModelInfo,
  SessionUpdate,
  SessionUpdateParams,
  StopReason,
  contentBlockText,
} from "./AcpProtocol";
import { WorkspaceChangeTracker, mergeTouchedFiles } from "./WorkspaceChangeTracker";

export interface DelegationRequest {
  agentId: string;
  prompt: string;
  /** Absolute; scopes both the agent's cwd and our file-access boundary. */
  workspacePath: string;
  /** Overrides any model pinned in the agent's config. */
  model?: string;
  taskId?: string;
  /**
   * What the delegation is expected to produce.
   *
   * - `"files"` (default): the agent works in the workspace with its own tools.
   * - `"text"`: the agent is told not to use tools at all and to answer in the
   *   turn itself; the caller persists `DelegationResult.text`.
   *
   * `"text"` exists because an agent's local tooling is a dependency you do not
   * control. Measured against Hermes, whose file/shell tools hang for ~420s each
   * under non-TTY ACP: the same document task failed at a 600s timeout as
   * `"files"` and finished in 74s as `"text"`. Prefer it whenever the output is
   * prose rather than edits to a repo.
   */
  deliverable?: "files" | "text";
  /**
   * How many times to run the turn before giving up. Only *transient* failures
   * are retried (see {@link isRetryableFailure}); a refusal or an exhausted
   * account fails immediately however high this is. Default 1 — no retry.
   */
  maxAttempts?: number;
  onText?: (text: string) => void;
  onToolCall?: (update: SessionUpdate) => void;
}

export interface DelegationResult {
  agentId: string;
  agentLabel: string;
  sessionId: string;
  taskId: string;
  /** Concatenated agent_message_chunk text for the turn. */
  text: string;
  stopReason: StopReason | "error";
  /** False when the turn ended without usable output — see `failureReason`. */
  ok: boolean;
  failureReason?: string;
  /**
   * Files the delegation changed: client fs/write calls plus writes the agent
   * made with its own tools, observed by watching the workspace.
   */
  touchedFiles: string[];
  toolCalls: Array<{ toolCallId: string; title?: string; kind?: string; status?: string }>;
  modelId?: string;
  availableModels?: ModelInfo[];
  usage?: { used?: number; size?: number };
  durationMs: number;
  /** 1 unless a transient failure was retried; counts the successful try too. */
  attempts: number;
}

export interface AcpSubagentDriverOptions {
  registry: AcpAgentCatalog;
  /** Builds the client half for a delegation; lets callers choose the UI. */
  createHost: (context: { spec: AcpAgentSpec; workspacePath: string }) => AcpClientHost & {
    touchedFiles?: string[];
  };
  log?: (message: string) => void;
}

/**
 * Turn text that means the agent failed despite reporting a clean stop.
 *
 * Hermes surfaces upstream provider failures as ordinary message content with
 * `stopReason: "end_turn"` (observed: `Billing or credits exhausted: HTTP 402`).
 * Trusting stopReason alone would record such a turn as a successful
 * delegation, so turn text is screened before the result is called ok.
 */
const UPSTREAM_FAILURE_PATTERNS: RegExp[] = [
  // Observed under provider throttling: "⚠️ No reply: the model returned
  // empty content after retries and any fallback providers."
  /\breturned empty content\b/i,
  /\bcredits? exhausted\b/i,
  /\bHTTP\s+4\d{2}\b/,
  /\bHTTP\s+5\d{2}\b/,
  /\brate limit(ed)?\b/i,
  /\binsufficient (credit|quota|balance)\b/i,
  /\bauthentication failed\b/i,
  /\binvalid api key\b/i,
];

/**
 * Failures worth trying again.
 *
 * The distinction is whether a second identical request could plausibly
 * succeed. A free or preview endpoint returning nothing under load can; an
 * exhausted account, a rejected key, or a model that refused the task cannot,
 * and retrying those only burns wall-clock before failing anyway. Note the
 * asymmetry with HTTP status: 429 and 5xx are transient, 402 is not.
 */
const RETRYABLE_FAILURE_PATTERNS: RegExp[] = [
  /\breturned empty content\b/i,
  /\bproduced no output\b/i,
  /\brate limit(ed)?\b/i,
  /\bHTTP\s+5\d{2}\b/,
  /\bHTTP\s+429\b/,
  /\btimed out\b/i,
];

/** Terminal regardless of the above; checked first. */
const TERMINAL_FAILURE_PATTERNS: RegExp[] = [
  /\bcredits? exhausted\b/i,
  /\binsufficient (credit|quota|balance)\b/i,
  /\bHTTP\s+402\b/,
  /\bauthentication failed\b/i,
  /\binvalid api key\b/i,
  /\brefused the request\b/i,
  /\bwas cancelled\b/i,
];

/**
 * Prepended for `deliverable: "text"`.
 *
 * Both halves matter: forbidding tools is what avoids the agent's local tooling,
 * and naming the turn as the deliverable is what stops it from ending with a
 * promise to write a file it never wrote.
 */
const TEXT_DELIVERABLE_PREAMBLE = [
  "Do not use any tools for this task — no file reads, no file writes, no shell, no browser.",
  "Everything you need is in this message, and your reply in this turn IS the deliverable:",
  "the caller saves it verbatim. Reply with the finished work only — no preamble, no closing",
  "remarks, and no commentary on your own compliance with these instructions.",
].join(" ");

/** Apply the delegation's deliverable shape to the prompt text. */
export function buildPrompt(prompt: string, deliverable: "files" | "text" | undefined): string {
  return deliverable === "text" ? `${TEXT_DELIVERABLE_PREAMBLE}\n\n${prompt}` : prompt;
}

/** Whether a failed delegation is worth another attempt. */
export function isRetryableFailure(failureReason: string | undefined): boolean {
  if (!failureReason) {
    return false;
  }
  if (TERMINAL_FAILURE_PATTERNS.some((pattern) => pattern.test(failureReason))) {
    return false;
  }
  return RETRYABLE_FAILURE_PATTERNS.some((pattern) => pattern.test(failureReason));
}

export class AcpSubagentDriver {
  private readonly _active = new Map<string, AcpConnection>();
  private _counter = 0;

  constructor(private readonly _options: AcpSubagentDriverOptions) {}

  listAgents(): AcpAgentConfig[] {
    return this._options.registry.list();
  }

  /** Ids of delegations currently running. */
  get activeDelegations(): string[] {
    return [...this._active.keys()];
  }

  /**
   * Run a delegation, retrying transient upstream failures.
   *
   * Each attempt is a fresh process and session: a retry must not inherit the
   * failed turn's context, and the agent has no partial state worth resuming.
   */
  async delegate(request: DelegationRequest): Promise<DelegationResult> {
    const maxAttempts = Math.max(1, request.maxAttempts ?? 1);
    let result = await this._attempt(request);
    let attempts = 1;

    while (attempts < maxAttempts && !result.ok && isRetryableFailure(result.failureReason)) {
      this._options.log?.(
        `[acp:${request.agentId}] attempt ${attempts}/${maxAttempts} failed transiently ` +
          `(${result.failureReason}); retrying`,
      );
      result = await this._attempt(request);
      attempts++;
    }

    if (!result.ok && attempts > 1) {
      this._options.log?.(`[acp:${request.agentId}] gave up after ${attempts} attempts`);
    }
    return { ...result, attempts };
  }

  private async _attempt(request: DelegationRequest): Promise<DelegationResult> {
    const started = Date.now();
    const config = this._options.registry.get(request.agentId);
    if (!config) {
      throw new AcpConnectionError(`No ACP agent configured with id '${request.agentId}'.`);
    }
    const spec = this._options.registry.toSpec(config, request.workspacePath);
    const host = this._options.createHost({ spec, workspacePath: request.workspacePath });
    const taskId = request.taskId ?? `acp-${++this._counter}`;

    // Agents may write through their own tools rather than fs/write_text_file
    // (Hermes does), so host bookkeeping alone under-counts touched files.
    const tracker = new WorkspaceChangeTracker(request.workspacePath, this._options.log);
    tracker.start();

    const text: string[] = [];
    const toolCalls = new Map<string, { toolCallId: string; title?: string; kind?: string; status?: string }>();
    let usage: { used?: number; size?: number } | undefined;

    const connection = new AcpConnection({
      spec,
      host,
      log: this._options.log,
      onSessionUpdate: (params: SessionUpdateParams) => {
        const update = params.update as any;
        switch (update?.sessionUpdate) {
          case "agent_message_chunk": {
            const chunk = contentBlockText(update.content as ContentBlock);
            if (chunk) {
              text.push(chunk);
              request.onText?.(chunk);
            }
            break;
          }
          case "tool_call":
          case "tool_call_update": {
            const id = String(update.toolCallId ?? "");
            if (id) {
              // tool_call_update carries only deltas; merge onto what we have.
              const existing = toolCalls.get(id) ?? { toolCallId: id };
              toolCalls.set(id, {
                ...existing,
                title: update.title ?? existing.title,
                kind: update.kind ?? existing.kind,
                status: update.status ?? existing.status,
              });
            }
            request.onToolCall?.(update);
            break;
          }
          case "usage_update":
            usage = { used: update.used, size: update.size };
            break;
          default:
            break;
        }
      },
    });

    this._active.set(taskId, connection);
    try {
      await connection.start();
      const { sessionId, models } = await connection.newSession(request.workspacePath);

      const wantedModel = request.model ?? config.model;
      let modelId = models?.currentModelId;
      if (wantedModel) {
        // Non-fatal: an agent may advertise no model surface at all, and a bad
        // pin should degrade to the agent's default rather than kill the task.
        try {
          await connection.setModel(sessionId, wantedModel);
          modelId = wantedModel;
        } catch (err) {
          this._options.log?.(
            `[acp:${spec.id}] could not pin model '${wantedModel}': ${(err as Error).message}`,
          );
        }
      }

      const turn = await connection.prompt(sessionId, [
        { type: "text", text: buildPrompt(request.prompt, request.deliverable) },
      ]);
      const joined = text.join("");
      const failureReason = assessTurn(turn.stopReason, joined);

      return {
        agentId: config.id,
        agentLabel: spec.label,
        sessionId,
        taskId,
        text: joined,
        stopReason: turn.stopReason,
        ok: failureReason === undefined,
        failureReason,
        touchedFiles: mergeTouchedFiles(
          (host as { touchedFiles?: string[] }).touchedFiles ?? [],
          await tracker.collect(),
        ),
        toolCalls: [...toolCalls.values()],
        modelId,
        availableModels: models?.availableModels,
        usage,
        durationMs: Date.now() - started,
        attempts: 1, // delegate() overwrites with the real count
      };
    } catch (err) {
      const detail = (err as Error).message;
      const stderr = connection.stderrTail;
      return {
        agentId: config.id,
        agentLabel: spec.label,
        sessionId: "",
        taskId,
        text: text.join(""),
        stopReason: "error",
        ok: false,
        failureReason: stderr.length ? `${detail} — agent stderr: ${stderr.slice(-3).join(" | ")}` : detail,
        touchedFiles: mergeTouchedFiles(
          (host as { touchedFiles?: string[] }).touchedFiles ?? [],
          await tracker.collect(),
        ),
        toolCalls: [...toolCalls.values()],
        usage,
        durationMs: Date.now() - started,
        attempts: 1, // delegate() overwrites with the real count
      };
    } finally {
      this._active.delete(taskId);
      connection.dispose();
      tracker.dispose();
    }
  }

  /** Cancel a running delegation; the process is reaped by delegate()'s finally. */
  cancel(taskId: string): boolean {
    const connection = this._active.get(taskId);
    if (!connection) {
      return false;
    }
    connection.dispose();
    return true;
  }

  /** Reap every live agent process. Must run on extension deactivate. */
  dispose(): void {
    for (const [, connection] of this._active) {
      connection.dispose();
    }
    this._active.clear();
  }
}

/**
 * Decide whether a finished turn actually succeeded.
 * Returns a reason string when it did not, or undefined when it did.
 */
export function assessTurn(stopReason: StopReason, text: string): string | undefined {
  if (stopReason === "cancelled") {
    return "The delegation was cancelled.";
  }
  if (stopReason === "refusal") {
    return "The agent refused the request.";
  }
  if (stopReason === "max_tokens") {
    return "The agent hit its output limit before finishing.";
  }
  if (stopReason === "max_turn_requests") {
    return "The agent hit its tool-call budget before finishing.";
  }
  const trimmed = text.trim();
  if (!trimmed) {
    return "The agent produced no output.";
  }
  if (UPSTREAM_FAILURE_PATTERNS.some((pattern) => pattern.test(trimmed))) {
    // Quote the agent's own words: the operator needs the provider's message,
    // not our paraphrase of it.
    return `The agent reported an upstream failure: ${firstLine(trimmed)}`;
  }
  return undefined;
}

function firstLine(text: string): string {
  const line = text.split("\n", 1)[0].trim();
  return line.length > 300 ? `${line.slice(0, 300)}…` : line;
}
