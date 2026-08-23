/**
 * Serves the client half of ACP against VS Code.
 *
 * This is where WaterFree's thesis shows up concretely: a subagent never writes
 * to the tree itself. It asks, and this class decides — applying a workspace
 * boundary check and routing anything sensitive through the user's approval.
 */

import * as path from "path";
import * as vscode from "vscode";
import { AcpClientHost } from "./AcpConnection";
import { AcpTerminalHost } from "./AcpTerminalHost";

export interface PermissionRequest {
  sessionId: string;
  agentLabel: string;
  toolCall?: { toolCallId?: string; title?: string; kind?: string };
  options: Array<{ optionId: string; name?: string; kind?: string }>;
}

export type PermissionResolver = (
  request: PermissionRequest,
) => Promise<{ optionId: string } | "cancelled">;

export interface WorkspaceClientHostOptions {
  /** Absolute workspace root; all file access is confined to it. */
  workspacePath: string;
  agentLabel: string;
  resolvePermission: PermissionResolver;
  log?: (message: string) => void;
  onFileWritten?: (absolutePath: string) => void;
  /**
   * Let the agent run shell commands. Off by default and opt-in per delegation:
   * running arbitrary commands is a strictly larger grant than editing files,
   * so the caller states it rather than inheriting it.
   */
  allowTerminal?: boolean;
  /** Asks the user to approve one command. Required when allowTerminal is set. */
  confirmCommand?: (command: string, args: string[], cwd: string) => Promise<boolean>;
}

export class WorkspaceAcpClientHost implements AcpClientHost {
  private readonly _touchedFiles = new Set<string>();
  /** Present only when the caller opted in; its presence is what advertises
   * `terminal: true` during the initialize handshake. */
  readonly terminal?: AcpTerminalHost;

  constructor(private readonly _options: WorkspaceClientHostOptions) {
    if (_options.allowTerminal) {
      const confirm = _options.confirmCommand;
      if (!confirm) {
        throw new Error("allowTerminal requires confirmCommand: commands must be approvable.");
      }
      this.terminal = new AcpTerminalHost({
        workspacePath: _options.workspacePath,
        confirm,
        log: _options.log,
      });
    }
  }

  /** Absolute paths the agent wrote during this session, in write order. */
  get touchedFiles(): string[] {
    return [...this._touchedFiles];
  }

  async readTextFile(params: {
    sessionId: string;
    path: string;
    line?: number;
    limit?: number;
  }): Promise<string> {
    const target = this._resolveInsideWorkspace(params.path);
    const bytes = await vscode.workspace.fs.readFile(vscode.Uri.file(target));
    const text = Buffer.from(bytes).toString("utf-8");

    // `line` is 1-based per spec; `limit` counts lines, not bytes.
    if (params.line === undefined && params.limit === undefined) {
      return text;
    }
    const lines = text.split("\n");
    const start = Math.max(0, (params.line ?? 1) - 1);
    const end = params.limit !== undefined ? start + params.limit : lines.length;
    return lines.slice(start, end).join("\n");
  }

  async writeTextFile(params: { sessionId: string; path: string; content: string }): Promise<void> {
    const target = this._resolveInsideWorkspace(params.path);
    const uri = vscode.Uri.file(target);
    // Create parent directories; agents assume POSIX-ish mkdir -p semantics.
    await vscode.workspace.fs.createDirectory(uri.with({ path: path.posix.dirname(uri.path) }));
    await vscode.workspace.fs.writeFile(uri, Buffer.from(params.content, "utf-8"));
    this._touchedFiles.add(target);
    this._options.onFileWritten?.(target);
    this._log(`wrote ${target}`);
  }

  async requestPermission(params: {
    sessionId: string;
    toolCall?: { toolCallId?: string; title?: string; kind?: string };
    options: Array<{ optionId: string; name?: string; kind?: string }>;
  }): Promise<{ optionId: string } | "cancelled"> {
    if (!params.options?.length) {
      // Nothing to choose from; refusing is safer than inventing an optionId.
      return "cancelled";
    }
    return this._options.resolvePermission({
      sessionId: params.sessionId,
      agentLabel: this._options.agentLabel,
      toolCall: params.toolCall,
      options: params.options,
    });
  }

  /**
   * Confine agent file access to the workspace.
   *
   * A subagent is driven by model output, so its paths are untrusted input: a
   * traversal or an absolute path elsewhere on disk must not be served just
   * because the protocol allows absolute paths.
   */
  private _resolveInsideWorkspace(candidate: string): string {
    const root = path.resolve(this._options.workspacePath);
    const target = path.resolve(root, candidate);
    const relative = path.relative(root, target);
    const escapes = relative.startsWith("..") || path.isAbsolute(relative);
    if (escapes) {
      throw new Error(
        `Refusing access outside the workspace: ${candidate} resolves to ${target}`,
      );
    }
    return target;
  }

  private _log(message: string): void {
    this._options.log?.(`[acp:host] ${message}`);
  }
}

/**
 * Default resolver: a modal in the same language as WaterFree's annotation
 * gesture, built from the agent's own options.
 *
 * Deliberately not a full approve/alter/redirect mapping. That gesture offers
 * "alter" — send the agent different instructions — and ACP's permission
 * response has no such outcome: the reply is one of the agent's own options, or
 * cancelled. Offering an Alter button that silently degraded to a cancel would
 * misrepresent what happened to the turn. Redirecting a delegation means
 * cancelling it and delegating again, which the caller already controls.
 *
 * What is shared with that gesture: a modal rather than a dismissible list (a
 * permission prompt clicked away by accident should not read as a decision),
 * the ✓/✗ glyph language, and the tool call shown as detail.
 *
 * Agent-supplied labels are rendered as data, never interpreted as instructions.
 */
export function createQuickPickPermissionResolver(): PermissionResolver {
  return async (request) => {
    const title = request.toolCall?.title || request.toolCall?.kind || "run a tool";
    // Modal buttons are order-sensitive and limited, so lead with the allow and
    // reject the agent actually offered rather than listing every variant.
    const allow = pickOption(request.options, ["allow", "approve", "accept", "yes"]);
    const reject = pickOption(request.options, ["reject", "deny", "no"]);

    const buttons: string[] = [];
    if (allow) { buttons.push(`✓ ${allow.name || "Approve"}`); }
    if (reject) { buttons.push(`✗ ${reject.name || "Reject"}`); }
    // An agent whose options match neither vocabulary still has to be answerable.
    const extras = request.options.filter((o) => o !== allow && o !== reject);
    for (const option of extras.slice(0, 4 - buttons.length)) {
      buttons.push(option.name || option.optionId);
    }

    const choice = await vscode.window.showWarningMessage(
      `${request.agentLabel} wants to ${title}.`,
      { modal: true, detail: describeToolCall(request) },
      ...buttons,
    );
    if (!choice) {
      // Dismissal is a refusal, not a default-allow.
      return "cancelled";
    }
    if (allow && choice === `✓ ${allow.name || "Approve"}`) {
      return { optionId: allow.optionId };
    }
    if (reject && choice === `✗ ${reject.name || "Reject"}`) {
      return { optionId: reject.optionId };
    }
    const matched = extras.find((o) => (o.name || o.optionId) === choice);
    return matched ? { optionId: matched.optionId } : "cancelled";
  };
}

/** First option whose kind or id contains any of `tokens`. */
function pickOption(
  options: Array<{ optionId: string; name?: string; kind?: string }>,
  tokens: string[],
): { optionId: string; name?: string; kind?: string } | undefined {
  return options.find((option) => {
    const haystack = `${option.kind ?? ""} ${option.optionId}`.toLowerCase();
    return tokens.some((token) => haystack.includes(token));
  });
}

function describeToolCall(request: PermissionRequest): string {
  const parts = [request.toolCall?.title, request.toolCall?.kind].filter(Boolean);
  return parts.length ? parts.join("\n") : "The agent did not describe this tool call.";
}

/**
 * Non-interactive resolver for headless/CI use.
 *
 * Picks the first option whose kind or id marks it as a one-shot allow, and
 * cancels when none does — never falls through to options[0], which on most
 * agents is not the conservative choice.
 */
export function createAutoApproveResolver(log?: (m: string) => void): PermissionResolver {
  return async (request) => {
    const allow = request.options.find((option) => {
      const token = `${option.kind ?? ""} ${option.optionId}`.toLowerCase();
      return token.includes("allow") || token.includes("approve") || token.includes("accept");
    });
    if (!allow) {
      log?.(`[acp:host] no allow-shaped option among [${request.options.map((o) => o.optionId).join(", ")}]; cancelling`);
      return "cancelled";
    }
    return { optionId: allow.optionId };
  };
}
