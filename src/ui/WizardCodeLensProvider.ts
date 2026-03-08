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

    const chunks = parseChunkMarkers(document);
    const isMarketResearch = context.stageId === "market_research";
    const isInitialIdeaDraft =
      isMarketResearch && !chunks.some((c) => c.id === "initial_goal");

    const lenses: vscode.CodeLens[] = [];
    const headerRange = new vscode.Range(0, 0, 0, 0);

    if (isInitialIdeaDraft) {
      lenses.push(
        new vscode.CodeLens(headerRange, {
          title: "▶ Submit",
          command: "waterfree.runWizardStep",
          arguments: [context],
        }),
        new vscode.CodeLens(headerRange, {
          title: "✦ Refine",
          command: "waterfree.refineWizardIdea",
          arguments: [context],
        }),
      );
      return lenses;
    }

    lenses.push(
      new vscode.CodeLens(headerRange, {
        title: isMarketResearch ? "Send for Refinement" : "Run Stage",
        command: "waterfree.runWizardStep",
        arguments: [context],
      }),
    );

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
      const separatorLine = findSeparatorLine(document);
      if (separatorLine >= 0) {
        const sepRange = document.lineAt(separatorLine).range;
        const initialGoalChunk = chunks.find((c) => c.id === "initial_goal");
        const isResolved = initialGoalChunk?.accepted ?? false;
        lenses.push(
          new vscode.CodeLens(sepRange, {
            title: isResolved ? "$(lock) Resolved" : "$(warning) Unresolved",
            command: "",
            arguments: [],
          }),
        );
      }
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

function findSeparatorLine(document: vscode.TextDocument): number {
  let frontmatterCount = 0;
  let frontmatterClosed = false;
  for (let i = 0; i < document.lineCount; i++) {
    const text = document.lineAt(i).text.trim();
    if (text === "---") {
      frontmatterCount++;
      if (frontmatterCount === 2) {
        frontmatterClosed = true;
        continue;
      }
      if (frontmatterClosed) {
        return i;
      }
    }
  }
  return -1;
}

export type { WizardDocContext };
