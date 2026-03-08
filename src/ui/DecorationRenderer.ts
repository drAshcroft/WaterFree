/**
 * DecorationRenderer — renders intent annotations as CodeLens items in the editor.
 *
 * For each pending/approved annotation targeting the currently open file,
 * a CodeLens appears at the target line showing:
 *    [WF] {summary}   [✓ Approve] [✎ Alter] [⟳ Redirect]
 *
 * Annotations are in-memory only — nothing is written to the file.
 */

import * as vscode from "vscode";
import {
  getAnnotationTargetLine,
  getTaskTargetPath,
  type AnnotationData,
  type TaskData,
} from "./PlanSidebar.js";

export interface AnnotationWithTask {
  annotation: AnnotationData;
  task: TaskData;
}

export class DecorationRenderer
  implements vscode.CodeLensProvider, vscode.Disposable
{
  private _annotations: AnnotationWithTask[] = [];
  private _workspacePath: string | null = null;
  private readonly _emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this._emitter.event;

  private readonly _registration: vscode.Disposable;

  constructor() {
    // Register for all file types — CodeLens only appears on lines with annotations
    this._registration = vscode.languages.registerCodeLensProvider(
      { scheme: "file" },
      this,
    );
  }

  /**
   * Called by the controller whenever the session state changes.
   * Replaces the full annotation set and triggers a refresh.
   */
  updateAnnotations(pairs: AnnotationWithTask[], workspacePath: string): void {
    this._annotations = pairs;
    this._workspacePath = workspacePath;
    this._emitter.fire();
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    const docPath = document.uri.fsPath;
    const lenses: vscode.CodeLens[] = [];

    for (const { annotation, task } of this._annotations) {
      if (!this._workspacePath) {
        continue;
      }

      const resolvedPath = getTaskTargetPath(task, this._workspacePath);
      if (!resolvedPath) {
        continue;
      }

      // Normalise path separators for comparison
      const targetPath = resolvedPath.replace(/\\/g, "/");
      const docNorm = docPath.replace(/\\/g, "/");
      if (!docNorm.endsWith(targetPath) && targetPath !== docNorm) {
        continue;
      }

      // Clamp line to document bounds (0-based)
      const line = Math.max(
        0,
        Math.min(document.lineCount - 1, getAnnotationTargetLine(annotation, task)),
      );
      const range = document.lineAt(line).range;

      // Header lens — status + summary
      const statusIcon = annotation.status === "approved" ? "$(check)" : "$(circle-outline)";
      lenses.push(
        new vscode.CodeLens(range, {
          title: `${statusIcon}  [WF] ${annotation.summary}`,
          command: "waterfree.showAnnotation",
          arguments: [task.id, annotation.id],
        }),
      );

      // Action lenses — only show for pending annotations
      if (annotation.status === "pending") {
        lenses.push(
          new vscode.CodeLens(range, {
            title: "✓ Approve",
            command: "waterfree.approveAnnotation",
            arguments: [annotation.id],
          }),
          new vscode.CodeLens(range, {
            title: "✎ Alter",
            command: "waterfree.alterAnnotation",
            arguments: [task.id, annotation.id],
          }),
          new vscode.CodeLens(range, {
            title: "⟳ Redirect",
            command: "waterfree.redirectTask",
            arguments: [task.id],
          }),
        );
      }
    }

    return lenses;
  }

  dispose(): void {
    this._registration.dispose();
    this._emitter.dispose();
  }
}
