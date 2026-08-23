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
}

export class WorkspaceAcpClientHost implements AcpClientHost {
  private readonly _touchedFiles = new Set<string>();

  constructor(private readonly _options: WorkspaceClientHostOptions) {}

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
 * Default resolver: a modal quick-pick built from the agent's own options.
 *
 * Options come from the agent, so labels are rendered as data. The mapping to
 * WaterFree's approve/alter/redirect gesture is left to callers that have an
 * annotation in flight; this is the standalone fallback.
 */
export function createQuickPickPermissionResolver(): PermissionResolver {
  return async (request) => {
    const title = request.toolCall?.title || request.toolCall?.kind || "run a tool";
    const items = request.options.map((option) => ({
      label: option.name || option.optionId,
      description: option.kind ?? "",
      optionId: option.optionId,
    }));
    const picked = await vscode.window.showQuickPick(items, {
      title: `${request.agentLabel} wants to ${title}`,
      placeHolder: "Choose how to respond",
      ignoreFocusOut: true,
    });
    return picked ? { optionId: picked.optionId } : "cancelled";
  };
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
