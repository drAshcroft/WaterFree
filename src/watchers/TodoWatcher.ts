/**
 * TodoWatcher — scans documents for `// TODO: [wf] ...` comments on save.
 *
 * When a developer writes a [wf]-tagged TODO anywhere in a file, it is
 * treated as a live instruction to the AI — not a regular comment.
 * The controller receives these and can queue them as task redirects.
 */

import * as vscode from "vscode";

export interface WfTodo {
  file: string;
  line: number;
  instruction: string;
}

export type TodoHandler = (todos: WfTodo[]) => void;

// Matches:   // TODO: [wf] <instruction>
//             # TODO: [wf] <instruction>
//            /* TODO: [wf] <instruction>
const WF_TODO_PATTERN = /\/\/\s*TODO:\s*\[wf\]\s*(.+)|#\s*TODO:\s*\[wf\]\s*(.+)/gi;

export class TodoWatcher implements vscode.Disposable {
  private readonly _disposables: vscode.Disposable[] = [];
  private _handler: TodoHandler | null = null;

  constructor() {
    this._disposables.push(
      vscode.workspace.onDidSaveTextDocument((doc) => this._onSave(doc)),
    );
  }

  onTodosFound(handler: TodoHandler): void {
    this._handler = handler;
  }

  scanDocument(doc: vscode.TextDocument): WfTodo[] {
    const todos: WfTodo[] = [];
    const text = doc.getText();
    const lines = text.split("\n");

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      WF_TODO_PATTERN.lastIndex = 0;
      const match = WF_TODO_PATTERN.exec(line);
      if (match) {
        const instruction = (match[1] ?? match[2] ?? "").trim();
        if (instruction) {
          todos.push({
            file: doc.uri.fsPath,
            line: i + 1,
            instruction,
          });
        }
      }
    }

    return todos;
  }

  private _onSave(doc: vscode.TextDocument): void {
    const todos = this.scanDocument(doc);
    if (todos.length > 0 && this._handler) {
      this._handler(todos);
    }
  }

  dispose(): void {
    this._disposables.forEach((d) => d.dispose());
  }
}
