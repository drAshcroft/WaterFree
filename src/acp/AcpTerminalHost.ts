/**
 * Serves the ACP `terminal/*` method family: lets a subagent run commands.
 *
 * Without this an agent degrades to file edits only — it can write a test but
 * never run it. The trade is that commands are far more dangerous than writes,
 * so every create goes through the same permission gate as a file write and the
 * command is reported verbatim rather than summarised.
 *
 * Lifecycle is the hard part. The agent owns terminal ids and is supposed to
 * release them, but an agent that dies mid-turn never will, so the host owns a
 * registry it can reap wholesale on session end. Processes are never left
 * running past the delegation that created them.
 */

import { ChildProcess, spawn } from "child_process";
import * as path from "path";
import {
  TerminalCreateParams,
  TerminalExitStatus,
  TerminalOutputResult,
} from "./AcpProtocol";

/** Default retained output per terminal; the agent may request less. */
const DEFAULT_OUTPUT_BYTE_LIMIT = 1_000_000;

/** Grace period between SIGTERM and SIGKILL when killing a terminal. */
const KILL_ESCALATION_MS = 2000;

export interface TerminalHostOptions {
  /** Absolute; every terminal's cwd must resolve inside this. */
  workspacePath: string;
  /**
   * Gate for running a command. Returning false rejects the create — the same
   * shape as the file-write gate so one policy covers both.
   */
  confirm: (command: string, args: string[], cwd: string) => Promise<boolean>;
  log?: (message: string) => void;
}

interface TerminalEntry {
  id: string;
  proc: ChildProcess;
  /** Retained tail of combined stdout+stderr. */
  output: string;
  truncated: boolean;
  byteLimit: number;
  exitStatus: TerminalExitStatus | null;
  /** Resolved when the process exits; awaited by wait_for_exit. */
  exited: Promise<TerminalExitStatus>;
  released: boolean;
}

export class AcpTerminalError extends Error {}

export class AcpTerminalHost {
  private readonly _terminals = new Map<string, TerminalEntry>();
  private _counter = 0;

  constructor(private readonly _options: TerminalHostOptions) {}

  get activeTerminalIds(): string[] {
    return [...this._terminals.keys()];
  }

  async create(params: TerminalCreateParams): Promise<string> {
    const command = String(params.command ?? "").trim();
    if (!command) {
      throw new AcpTerminalError("terminal/create requires a command.");
    }
    const args = (params.args ?? []).map((a) => String(a));
    const cwd = this._resolveCwd(params.cwd);

    if (!(await this._options.confirm(command, args, cwd))) {
      throw new AcpTerminalError(`Running '${command}' was declined.`);
    }

    const env = { ...process.env };
    for (const entry of params.env ?? []) {
      if (entry && typeof entry.name === "string") {
        env[entry.name] = String(entry.value ?? "");
      }
    }

    // shell:false — the agent supplies command and args separately, and running
    // them through a shell would re-introduce quoting and injection problems the
    // structured form exists to avoid.
    const proc = spawn(command, args, {
      cwd,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });

    const id = `term-${++this._counter}`;
    const entry: TerminalEntry = {
      id,
      proc,
      output: "",
      truncated: false,
      byteLimit: normalizeByteLimit(params.outputByteLimit),
      exitStatus: null,
      released: false,
      exited: new Promise<TerminalExitStatus>((resolve) => {
        proc.on("exit", (code, signal) => {
          const status: TerminalExitStatus = { exitCode: code, signal: signal ?? null };
          entry.exitStatus = status;
          resolve(status);
        });
        // A command that cannot start never emits "exit"; without this the
        // agent's wait_for_exit would hang for the rest of the turn.
        proc.on("error", (err) => {
          this._options.log?.(`[terminal:${id}] failed to start: ${err.message}`);
          appendOutput(entry, `${err.message}\n`);
          const status: TerminalExitStatus = { exitCode: null, signal: null };
          entry.exitStatus = status;
          resolve(status);
        });
      }),
    };

    proc.stdout?.setEncoding("utf-8");
    proc.stdout?.on("data", (chunk: string) => appendOutput(entry, chunk));
    proc.stderr?.setEncoding("utf-8");
    proc.stderr?.on("data", (chunk: string) => appendOutput(entry, chunk));

    this._terminals.set(id, entry);
    this._options.log?.(`[terminal:${id}] spawned ${command} ${args.join(" ")} in ${cwd}`);
    return id;
  }

  output(terminalId: string): TerminalOutputResult {
    const entry = this._require(terminalId);
    return {
      output: entry.output,
      truncated: entry.truncated,
      exitStatus: entry.exitStatus,
    };
  }

  async waitForExit(terminalId: string): Promise<TerminalExitStatus> {
    return this._require(terminalId).exited;
  }

  /** Terminate the process but keep its output readable until released. */
  async kill(terminalId: string): Promise<void> {
    const entry = this._require(terminalId);
    if (entry.exitStatus) {
      return;
    }
    killTree(entry.proc);
    await entry.exited;
  }

  /** Drop a terminal entirely; kills it first if still running. */
  async release(terminalId: string): Promise<void> {
    const entry = this._terminals.get(terminalId);
    if (!entry) {
      return;
    }
    entry.released = true;
    if (!entry.exitStatus) {
      killTree(entry.proc);
    }
    this._terminals.delete(terminalId);
    this._options.log?.(`[terminal:${terminalId}] released`);
  }

  /**
   * Reap every terminal. Must run when the delegation ends — an agent that
   * crashes mid-turn releases nothing, and a leaked build process outlives the
   * session that asked for it.
   */
  disposeAll(): void {
    for (const [id, entry] of this._terminals) {
      if (!entry.exitStatus) {
        killTree(entry.proc);
        this._options.log?.(`[terminal:${id}] killed on session end`);
      }
    }
    this._terminals.clear();
  }

  private _require(terminalId: string): TerminalEntry {
    const entry = this._terminals.get(terminalId);
    if (!entry) {
      throw new AcpTerminalError(`No such terminal '${terminalId}'.`);
    }
    return entry;
  }

  /** Same boundary rule as file access: never outside the workspace. */
  private _resolveCwd(requested?: string): string {
    const root = path.resolve(this._options.workspacePath);
    if (!requested) {
      return root;
    }
    const target = path.resolve(root, requested);
    const rel = path.relative(root, target);
    if (rel.startsWith("..") || path.isAbsolute(rel)) {
      throw new AcpTerminalError(`Refusing to run outside the workspace: ${requested}`);
    }
    return target;
  }
}

function normalizeByteLimit(requested: number | undefined): number {
  if (typeof requested !== "number" || !Number.isFinite(requested) || requested <= 0) {
    return DEFAULT_OUTPUT_BYTE_LIMIT;
  }
  return Math.min(requested, DEFAULT_OUTPUT_BYTE_LIMIT);
}

/**
 * Append output, trimming from the front past the limit.
 *
 * The tail is kept rather than the head because the useful part of a failed
 * build or test run is at the end.
 */
function appendOutput(entry: TerminalEntry, chunk: string): void {
  entry.output += chunk;
  if (entry.output.length > entry.byteLimit) {
    entry.output = entry.output.slice(entry.output.length - entry.byteLimit);
    entry.truncated = true;
  }
}

/**
 * Kill a process, escalating if it ignores the polite signal.
 *
 * Build tools spawn children; on POSIX the negative pid targets the whole
 * process group so those children die too. Windows has no equivalent here, so
 * the direct kill is the best available.
 */
function killTree(proc: ChildProcess): void {
  if (proc.exitCode !== null || proc.killed) {
    return;
  }
  try {
    proc.kill();
  } catch {
    /* already gone */
  }
  setTimeout(() => {
    if (proc.exitCode === null && !proc.killed) {
      try {
        proc.kill("SIGKILL");
      } catch {
        /* already gone */
      }
    }
  }, KILL_ESCALATION_MS).unref?.();
}
