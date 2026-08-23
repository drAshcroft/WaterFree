/**
 * Records files changed under a workspace while a delegation runs.
 *
 * Exists because agents ask permission through ACP but then perform the edit
 * with their own local tools — observed with Hermes, which requests
 * "Approve edit: <file>" and, once allowed, writes via its builtin tooling, so
 * `fs/write_text_file` never reaches the client and host-reported touchedFiles
 * under-counts. Watching the directory catches writes regardless of which path
 * the agent used.
 *
 * Best-effort by design: recursive fs.watch is native on Windows/macOS and
 * needs Node >= 20 on Linux; where unavailable the tracker is inert rather
 * than fatal. It can also attribute writes made by unrelated processes during
 * the delegation window — acceptable for reporting, which is why obvious
 * non-agent noise (VCS metadata, dependency trees) is filtered.
 */

import * as fs from "fs";
import * as path from "path";

const IGNORED_SEGMENTS = new Set([".git", ".hg", ".svn", "node_modules", "__pycache__"]);

/** Events land asynchronously; a final write can trail the turn's end. */
const DEFAULT_SETTLE_MS = 250;

export class WorkspaceChangeTracker {
  private _watcher: fs.FSWatcher | null = null;
  private readonly _root: string;
  private readonly _paths = new Set<string>();

  constructor(root: string, private readonly _log?: (message: string) => void) {
    this._root = path.resolve(root);
  }

  start(): void {
    if (this._watcher) {
      return;
    }
    try {
      this._watcher = fs.watch(this._root, { recursive: true }, (_event, filename) => {
        if (!filename) {
          return;
        }
        const rel = filename.toString();
        if (rel.split(/[\\/]/).some((segment) => IGNORED_SEGMENTS.has(segment))) {
          return;
        }
        this._paths.add(path.join(this._root, rel));
      });
      // A watcher error (e.g. the root vanishing) must degrade, not throw
      // uncaught on the event loop.
      this._watcher.on("error", (err) => {
        this._log?.(`[change-tracker] watcher error, tracking degraded: ${err.message}`);
        this.dispose();
      });
    } catch (err) {
      this._log?.(`[change-tracker] unavailable (${(err as Error).message}); touchedFiles will rely on fs/write only`);
      this._watcher = null;
    }
  }

  /**
   * Stop watching and return the evented paths that exist as files now.
   * Waits briefly first so a write racing the turn's end is still seen.
   */
  async collect(settleMs: number = DEFAULT_SETTLE_MS): Promise<string[]> {
    if (this._watcher && settleMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, settleMs));
    }
    this.dispose();
    const files: string[] = [];
    for (const candidate of this._paths) {
      try {
        if (fs.statSync(candidate).isFile()) {
          files.push(candidate);
        }
      } catch {
        /* deleted or unreadable since the event — not a touched file we can report */
      }
    }
    return files.sort();
  }

  dispose(): void {
    this._watcher?.close();
    this._watcher = null;
  }
}

/**
 * Host-reported writes first (they are authoritative and ordered), then
 * watcher-only extras. Deduped case-insensitively: Windows paths from the two
 * sources can differ only by drive-letter or directory casing.
 */
export function mergeTouchedFiles(hostReported: string[], watched: string[]): string[] {
  const seen = new Set<string>();
  const merged: string[] = [];
  for (const file of [...hostReported, ...watched]) {
    const key = path.resolve(file).toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      merged.push(file);
    }
  }
  return merged;
}
