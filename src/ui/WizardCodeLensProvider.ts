import * as vscode from "vscode";
import {
  parseChunkMarkers,
  parseWizardDocContextFromDocument,
  type WizardDocContext,
} from "../wizard/WizardDocState.js";

export class WizardCodeLensProvider implements vscode.CodeLensProvider, vscode.Disposable {
  private readonly _emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this._emitter.event;
  private readonly _disposables: vscode.Disposable[] = [];
  private readonly _registration: vscode.Disposable;

  constructor() {
    this._registration = vscode.languages.registerCodeLensProvider(
      { scheme: "file", language: "markdown" },
      this,
    );
    this._disposables.push(
      vscode.workspace.onDidChangeTextDocument(() => this._emitter.fire()),
      vscode.workspace.onDidOpenTextDocument(() => this._emitter.fire()),
      vscode.window.onDidChangeActiveTextEditor(() => this._emitter.fire()),
    );
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    const context = parseWizardDocContextFromDocument(document);
    if (!context) {
      return [];
    }

    const text = document.getText();
    const chunks = parseChunkMarkers(document);
    const isMarketResearch = context.stageId === "market_research";
    const isInitialIdeaDraft =
      isMarketResearch &&
      text.includes("# What is your idea? (describe in detail)");

    const lenses: vscode.CodeLens[] = [];
    const headerRange = new vscode.Range(0, 0, 0, 0);
    lenses.push(
      new vscode.CodeLens(headerRange, {
        title: isMarketResearch ? "Send for Refinement" : "Run Stage",
        command: "waterfree.runWizardStep",
        arguments: [context],
      }),
    );

    if (!isInitialIdeaDraft) {
      lenses.push(
        new vscode.CodeLens(headerRange, {
          title: "Accept Stage",
          command: "waterfree.acceptWizardStep",
          arguments: [context],
        }),
        new vscode.CodeLens(headerRange, {
          title: "Promote Todos",
          command: "waterfree.promoteWizardTodos",
          arguments: [context],
        }),
      );
    }

    if (context.stageId === "coding_agents") {
      lenses.push(
        new vscode.CodeLens(headerRange, {
          title: "Start Coding",
          command: "waterfree.startWizardCoding",
          arguments: [context],
        }),
      );
    }

    if (context.stageId === "review") {
      lenses.push(
        new vscode.CodeLens(headerRange, {
          title: "Run Review",
          command: "waterfree.runWizardReview",
          arguments: [context],
        }),
      );
    }

    if (isMarketResearch) {
      return lenses;
    }

    for (const chunk of chunks) {
      const range = document.lineAt(chunk.line).range;
      if (chunk.accepted) {
        lenses.push(
          new vscode.CodeLens(range, {
            title: "$(lock) Accepted",
            command: "waterfree.openWizard",
            arguments: [{ wizardId: context.wizardId, goal: "", persona: context.wizardId === "bring_idea_to_life" ? "architect" : "default" }],
          }),
        );
      } else if (chunk.hasDraft) {
        lenses.push(
          new vscode.CodeLens(range, {
            title: "Accept Chunk",
            command: "waterfree.acceptWizardChunk",
            arguments: [context, chunk.id],
          }),
        );
      }

      if (!chunk.accepted && chunk.hasDraft) {
        lenses.push(
          new vscode.CodeLens(range, {
            title: "Revise Chunk",
            command: "waterfree.reviseWizardChunk",
            arguments: [context, chunk.id],
          }),
        );
      }
    }

    return lenses;
  }

  dispose(): void {
    this._registration.dispose();
    this._emitter.dispose();
    for (const disposable of this._disposables) {
      disposable.dispose();
    }
  }
}

export type { WizardDocContext };
