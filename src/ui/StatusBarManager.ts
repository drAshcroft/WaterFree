/**
 * StatusBarManager — shows the current AI state in the VS Code status bar.
 * Clicking the item opens the WaterFree plan sidebar.
 */

import * as vscode from "vscode";

const STATE_LABELS: Record<string, string> = {
  idle: "$(hubot) WaterFree",
  planning: "$(loading~spin) Planning…",
  annotating: "$(edit) Annotating…",
  awaiting_review: "$(comment-discussion) Awaiting review",
  executing: "$(run) Executing…",
  scanning: "$(search) Scanning side effects…",
  answering: "$(comment) Answering…",
  awaiting_redirect: "$(sync) Awaiting redirect…",
};

const STATE_TOOLTIPS: Record<string, string> = {
  idle: "WaterFree — click to open plan",
  planning: "Generating implementation plan…",
  annotating: "Writing intent annotation — standby",
  awaiting_review: "Review the annotation and Approve, Alter, or Redirect",
  executing: "Writing code — do not edit files",
  scanning: "Checking for side effects after last edit",
  answering: "AI is answering your question",
  awaiting_redirect: "Processing your redirect instruction",
};

export class StatusBarManager implements vscode.Disposable {
  private readonly _item: vscode.StatusBarItem;

  constructor() {
    this._item = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Left,
      100,
    );
    this._item.command = "waterfree.openSidebar";
    this.setState("idle");
    this._item.show();
  }

  setState(state: string): void {
    this._item.text = STATE_LABELS[state] ?? `$(hubot) ${state}`;
    this._item.tooltip = STATE_TOOLTIPS[state] ?? state;

    // Colour coding: orange when waiting for human, default otherwise
    this._item.backgroundColor =
      state === "awaiting_review"
        ? new vscode.ThemeColor("statusBarItem.warningBackground")
        : undefined;

    // Set VS Code context so keybinding `when` clauses work
    void vscode.commands.executeCommand(
      "setContext",
      "waterfree.awaitingReview",
      state === "awaiting_review",
    );
  }

  setError(message: string): void {
    this._item.text = `$(error) WaterFree: error`;
    this._item.tooltip = message;
    this._item.backgroundColor = new vscode.ThemeColor(
      "statusBarItem.errorBackground",
    );
  }

  dispose(): void {
    this._item.dispose();
  }
}
