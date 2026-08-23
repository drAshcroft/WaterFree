/**
 * Catalog of ACP agents WaterFree can drive.
 *
 * ACP is a standard, so this is deliberately not Hermes-specific: any agent
 * that speaks ACP over stdio can be added by configuration alone. Hermes ships
 * as the built-in default because it is the one verified against this client.
 */

import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { AcpAgentCatalog, AcpAgentConfig, AcpAgentSpec } from "./AcpConnection";

export { AcpAgentConfig };

/**
 * Hermes exposes ACP via `hermes acp`. HERMES_ACCEPT_HOOKS is set because a
 * subagent runs without a TTY: without it, an unseen shell hook blocks forever
 * on a prompt nobody can answer.
 */
const HERMES_DEFAULT: AcpAgentConfig = {
  id: "hermes",
  label: "Hermes Agent",
  command: "hermes",
  args: ["acp"],
  env: { HERMES_ACCEPT_HOOKS: "1" },
  enabled: true,
};

const BUILT_IN_AGENTS: AcpAgentConfig[] = [HERMES_DEFAULT];

export class AcpAgentRegistry implements AcpAgentCatalog {
  constructor(private readonly _workspacePath: string) {}

  /** Built-ins merged with (and overridable by) user configuration. */
  list(): AcpAgentConfig[] {
    const configured = vscode.workspace
      .getConfiguration("waterfree")
      .get<AcpAgentConfig[]>("acpAgents", []);

    const byId = new Map<string, AcpAgentConfig>();
    for (const agent of BUILT_IN_AGENTS) {
      byId.set(agent.id, { ...agent });
    }
    for (const raw of configured) {
      const normalized = normalizeAgent(raw);
      if (!normalized) {
        continue;
      }
      // A user entry with a built-in id patches it rather than duplicating it,
      // so overriding just the model does not require restating the command.
      const existing = byId.get(normalized.id);
      byId.set(normalized.id, existing ? { ...existing, ...normalized } : normalized);
    }
    return [...byId.values()].filter((agent) => agent.enabled !== false);
  }

  get(agentId: string): AcpAgentConfig | undefined {
    return this.list().find((agent) => agent.id === agentId);
  }

  /** Resolve a config entry into something spawnable. */
  toSpec(config: AcpAgentConfig, cwd?: string): AcpAgentSpec {
    return {
      id: config.id,
      label: config.label || config.id,
      command: expandHome(config.command),
      args: [...config.args],
      env: config.env,
      cwd: cwd ?? this._workspacePath,
    };
  }
}

function normalizeAgent(raw: unknown): AcpAgentConfig | null {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const value = raw as Record<string, unknown>;
  const id = typeof value.id === "string" ? value.id.trim() : "";
  if (!id) {
    return null;
  }
  const command = typeof value.command === "string" ? value.command.trim() : "";
  const args = Array.isArray(value.args)
    ? value.args.filter((a): a is string => typeof a === "string")
    : [];
  const config: AcpAgentConfig = {
    id,
    label: typeof value.label === "string" && value.label.trim() ? value.label.trim() : id,
    command,
    args,
    enabled: value.enabled !== false,
  };
  if (value.env && typeof value.env === "object") {
    config.env = value.env as Record<string, string>;
  }
  if (typeof value.model === "string" && value.model.trim()) {
    config.model = value.model.trim();
  }
  // A patch of a built-in may legitimately omit command/args.
  if (!config.command) {
    delete (config as Partial<AcpAgentConfig>).command;
  }
  if (args.length === 0) {
    delete (config as Partial<AcpAgentConfig>).args;
  }
  return config as AcpAgentConfig;
}

function expandHome(command: string): string {
  if (command.startsWith("~/") || command.startsWith("~\\")) {
    return path.join(os.homedir(), command.slice(2));
  }
  return command;
}
