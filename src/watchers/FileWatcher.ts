/**
 * FileWatcher — notifies the Python backend when source files change
 * so the codebase index stays current.
 *
 * Debounces rapid saves to avoid spamming the indexer during large edits.
 */

import * as vscode from "vscode";
import type { PythonBridge } from "../bridge/PythonBridge.js";

const DEBOUNCE_MS = 800;

export class FileWatcher implements vscode.Disposable {
  private readonly _watcher: vscode.FileSystemWatcher;
  private readonly _disposables: vscode.Disposable[] = [];
  private _debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private _pendingUpdates = new Set<string>();
  private _pendingDeletes = new Set<string>();

  constructor(
    private readonly _bridge: PythonBridge,
    private readonly _workspacePath: string,
  ) {
    // Watch all source files except the .waterfree dir and node_modules
    this._watcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(_workspacePath, "**/*.{ts,tsx,js,jsx,py,go,rs,cs,java,rb,cpp,c}"),
    );

    this._disposables.push(
      this._watcher.onDidChange((uri) => this._onChanged(uri)),
      this._watcher.onDidCreate((uri) => this._onChanged(uri)),
      this._watcher.onDidDelete((uri) => this._onDeleted(uri)),
    );
  }

  private _onChanged(uri: vscode.Uri): void {
    const p = uri.fsPath;
    if (this._shouldIgnore(p)) {
      return;
    }
    this._pendingUpdates.add(p);
    this._scheduleFlush();
  }

  private _onDeleted(uri: vscode.Uri): void {
    const p = uri.fsPath;
    if (this._shouldIgnore(p)) {
      return;
    }
    this._pendingUpdates.delete(p);
    this._pendingDeletes.add(p);
    this._scheduleFlush();
  }

  private _scheduleFlush(): void {
    if (this._debounceTimer) {
      clearTimeout(this._debounceTimer);
    }
    this._debounceTimer = setTimeout(() => this._flush(), DEBOUNCE_MS);
  }

  private _flush(): void {
    const updates = [...this._pendingUpdates];
    const deletes = [...this._pendingDeletes];
    this._pendingUpdates.clear();
    this._pendingDeletes.clear();

    for (const p of updates) {
      void this._bridge
        .request("updateFile", { path: p, workspacePath: this._workspacePath })
        .catch(() => {/* backend may not support this yet */});
    }
    for (const p of deletes) {
      void this._bridge
        .request("removeFile", { path: p, workspacePath: this._workspacePath })
        .catch(() => {/* backend may not support this yet */});
    }
  }

  private _shouldIgnore(p: string): boolean {
    return (
      p.includes(".waterfree") ||
      p.includes("node_modules") ||
      p.includes("dist") ||
      p.includes("__pycache__")
    );
  }

  dispose(): void {
    if (this._debounceTimer) {
      clearTimeout(this._debounceTimer);
    }
    this._watcher.dispose();
    this._disposables.forEach((d) => d.dispose());
  }
}
