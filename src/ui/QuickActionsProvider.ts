/**
 * QuickActionsProvider — context-sensitive programming task buttons.
 *
 * Displays a second TreeView panel ("Quick Actions") in the WaterFree
 * sidebar. The buttons shown adapt to the language of the currently active
 * editor file. Clicking a button either:
 *   a) Immediately inserts a structured [wf] TODO comment at the top of the
 *      active file, or
 *   b) Opens a guided wizard (multi-step showInputBox) that builds a richer
 *      TODO before inserting it.
 *
 * The TodoWatcher picks up [wf] TODOs on the next file-save and queues them
 * as instructions to the AI — no backend round-trip required for the button
 * press itself.
 */

import * as vscode from "vscode";
import { commentChar } from "./quickactions/language-config.js";
import { TODOS } from "./quickactions/todo-templates.js";

// ------------------------------------------------------------------
// Action definitions
// ------------------------------------------------------------------

interface ActionDef {
  id: string;
  label: string;
  icon: string;          // VS Code codicon name (without "$()")
  description?: string;
  languages?: string[];  // if set, only show for these languageIds
  wizard?: true;         // if set, runs the wizard named by id
}

const ALL_ACTIONS: ActionDef[] = [
  // ---- Always visible -----------------------------------------------
  {
    id: "explainFile",
    label: "Explain this file",
    icon: "question",
    description: "Plain-English summary of what this file does",
  },
  {
    id: "wtf",
    label: "WTF happened here",
    icon: "flame",
    description: "Blunt audit — what's bad, what needs fixing, and why",
  },
  // ---- Language-specific --------------------------------------------
  {
    id: "cleanupCode",
    label: "Clean up this file",
    icon: "sparkle",
    description: "Remove dead code, fix naming, sort imports — no logic changes",
    languages: ["python", "typescript", "javascript", "typescriptreact", "javascriptreact", "csharp", "go", "rust"],
  },
  {
    id: "documentCode",
    label: "Document this file",
    icon: "book",
    description: "Add docstrings / JSDoc to public functions and classes",
    languages: ["python", "typescript", "javascript", "typescriptreact", "javascriptreact", "csharp"],
    wizard: true,
  },
  {
    id: "buildTestSuite",
    label: "Build test suite",
    icon: "beaker",
    description: "BDD-style guided test creation wizard",
    languages: ["python", "typescript", "javascript", "typescriptreact", "javascriptreact", "csharp"],
    wizard: true,
  },
  {
    id: "findBugs",
    label: "Find bugs & code smells",
    icon: "bug",
    description: "Identify anti-patterns, logic errors, and potential crashes",
    languages: ["python", "typescript", "javascript", "typescriptreact", "javascriptreact", "csharp", "go", "rust"],
  },
  {
    id: "reviewTypeSafety",
    label: "Review type safety",
    icon: "shield",
    description: "Find unsafe casts, any types, and missing validation",
    languages: ["typescript", "typescriptreact"],
  },
  {
    id: "solidCheck",
    label: "Review SOLID principles",
    icon: "layers",
    description: "Flag design violations — SRP, OCP, LSP, ISP, DIP",
    languages: ["csharp", "java"],
  },
  {
    id: "securityAudit",
    label: "Security audit",
    icon: "lock",
    description: "Check for injections, hardcoded secrets, and auth flaws",
    languages: ["python", "typescript", "javascript", "typescriptreact", "javascriptreact", "csharp", "go", "rust"],
  },
];

// ------------------------------------------------------------------
// Tree item
// ------------------------------------------------------------------

class QuickActionItem extends vscode.TreeItem {
  constructor(
    public readonly def: ActionDef,
    public readonly languageId: string,
  ) {
    super(def.label, vscode.TreeItemCollapsibleState.None);
    this.description = def.description;
    this.iconPath = new vscode.ThemeIcon(def.icon);
    this.tooltip = def.description;
    this.command = {
      command: "waterfree.quickAction",
      title: def.label,
      arguments: [def.id],
    };
  }
}

// ------------------------------------------------------------------
// Provider
// ------------------------------------------------------------------

export class QuickActionsProvider
  implements vscode.TreeDataProvider<QuickActionItem>, vscode.Disposable
{
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<undefined>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private _currentLanguageId = "plaintext";
  private _currentEditor: vscode.TextEditor | undefined;
  private readonly _disposables: vscode.Disposable[] = [];

  constructor() {
    // Refresh when the active editor changes
    this._disposables.push(
      vscode.window.onDidChangeActiveTextEditor((editor) => {
        this._currentEditor = editor;
        this._currentLanguageId = editor?.document.languageId ?? "plaintext";
        this._onDidChangeTreeData.fire(undefined);
      }),
    );

    // Seed with current editor
    this._currentEditor = vscode.window.activeTextEditor;
    this._currentLanguageId = this._currentEditor?.document.languageId ?? "plaintext";
  }

  getTreeItem(item: QuickActionItem): vscode.TreeItem {
    return item;
  }

  getChildren(): QuickActionItem[] {
    const lang = this._currentLanguageId;
    return ALL_ACTIONS
      .filter((a) => !a.languages || a.languages.includes(lang))
      .map((a) => new QuickActionItem(a, lang));
  }

  // ------------------------------------------------------------------
  // Action dispatch — called by extension command
  // ------------------------------------------------------------------

  async runAction(actionId: string): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      void vscode.window.showWarningMessage(
        "WaterFree Quick Actions: Open a file first.",
      );
      return;
    }

    const lang = editor.document.languageId;
    const cc = commentChar(lang);

    switch (actionId) {
      case "cleanupCode":     return this._insertTodo(editor, cc, TODOS.cleanup(lang));
      case "findBugs":        return this._insertTodo(editor, cc, TODOS.findBugs());
      case "wtf":             return this._insertTodo(editor, cc, TODOS.wtf());
      case "explainFile":     return this._insertTodo(editor, cc, TODOS.explain());
      case "reviewTypeSafety":return this._insertTodo(editor, cc, TODOS.typeSafety());
      case "solidCheck":      return this._insertTodo(editor, cc, TODOS.solid());
      case "securityAudit":   return this._insertTodo(editor, cc, TODOS.security());
      case "documentCode":    return this._wizardDocument(editor, cc, lang);
      case "buildTestSuite":  return this._wizardTestSuite(editor, cc, lang);
      default:
        void vscode.window.showWarningMessage(`Unknown quick action: ${actionId}`);
    }
  }

  // ------------------------------------------------------------------
  // Wizards
  // ------------------------------------------------------------------

  private async _wizardDocument(
    editor: vscode.TextEditor,
    cc: string,
    lang: string,
  ): Promise<void> {
    if (lang === "python") {
      const style = await vscode.window.showQuickPick(
        [
          { label: "NumPy", description: "Parameters / Returns / Raises / Examples" },
          { label: "Google", description: "Args / Returns / Raises" },
          { label: "Sphinx", description: ":param: / :type: / :returns:" },
        ],
        { title: "Document this file — Step 1 of 2: Docstring style?" },
      );
      if (!style) { return; }

      const scope = await vscode.window.showQuickPick(
        [
          { label: "All public functions and classes", picked: true },
          { label: "All functions including private" },
          { label: "Module docstring only" },
        ],
        {
          title: "Document this file — Step 2 of 2: What to document?",
          canPickMany: false,
        },
      );
      if (!scope) { return; }

      return this._insertTodo(editor, cc, TODOS.documentPython(style.label, scope.label));
    }

    // TypeScript / JavaScript
    const scope = await vscode.window.showQuickPick(
      [
        { label: "All exported functions and classes", picked: true },
        { label: "All functions including private" },
        { label: "All interfaces and types too" },
      ],
      {
        title: "Add JSDoc — What to document?",
        canPickMany: false,
      },
    );
    if (!scope) { return; }

    return this._insertTodo(editor, cc, TODOS.documentJs(scope.label));
  }

  private async _wizardTestSuite(
    editor: vscode.TextEditor,
    cc: string,
    lang: string,
  ): Promise<void> {
    const target = await vscode.window.showInputBox({
      title: "Build test suite — Step 1 of 4: What are you testing?",
      prompt: "Function name, class, or module",
      placeHolder: "e.g. UserAuthService.login or parse_config()",
      validateInput: (v) => (v.trim() ? null : "Required"),
    });
    if (target === undefined) { return; }

    const happyPath = await vscode.window.showInputBox({
      title: "Build test suite — Step 2 of 4: Describe the happy path.",
      prompt: "What happens when everything works?",
      placeHolder: "e.g. Valid credentials return a JWT token with correct claims",
      validateInput: (v) => (v.trim() ? null : "Required"),
    });
    if (happyPath === undefined) { return; }

    const edgeCasesRaw = await vscode.window.showInputBox({
      title: "Build test suite — Step 3 of 4: Edge cases (comma-separated, or leave blank).",
      placeHolder: "e.g. empty password, expired account, SQL injection in username",
    });
    if (edgeCasesRaw === undefined) { return; }

    const defaultFramework = lang === "python" ? "pytest" : "Jest/Vitest";
    const framework = await vscode.window.showInputBox({
      title: `Build test suite — Step 4 of 4: Testing framework?`,
      value: defaultFramework,
      placeHolder: defaultFramework,
    });
    if (framework === undefined) { return; }

    const edgeCases = edgeCasesRaw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    return this._insertTodo(
      editor,
      cc,
      TODOS.testSuite(target.trim(), happyPath.trim(), edgeCases, framework.trim(), lang),
    );
  }

  // ------------------------------------------------------------------
  // TODO insertion
  // ------------------------------------------------------------------

  private async _insertTodo(
    editor: vscode.TextEditor,
    cc: string,
    lines: string[],
  ): Promise<void> {
    const comment = lines.map((l) => (l === "" ? cc : `${cc} ${l}`)).join("\n");
    const insertion = comment + "\n\n";

    const success = await editor.edit((editBuilder) => {
      editBuilder.insert(new vscode.Position(0, 0), insertion);
    });

    if (success) {
      void vscode.window.showInformationMessage(
        "WaterFree: Quick action TODO inserted. Save the file to queue it.",
      );
      // Move cursor to line 0 so the TODO is visible
      const topPos = new vscode.Position(0, 0);
      editor.selection = new vscode.Selection(topPos, topPos);
      editor.revealRange(new vscode.Range(topPos, topPos));
    }
  }

  // ------------------------------------------------------------------
  // Disposal
  // ------------------------------------------------------------------

  dispose(): void {
    this._disposables.forEach((d) => d.dispose());
    this._onDidChangeTreeData.dispose();
  }
}
