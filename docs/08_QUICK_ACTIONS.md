# WaterFree — Quick Actions Sidebar
## Context-Sensitive Programming Task Buttons

---

## Overview

The Quick Actions panel lives in the WaterFree sidebar, directly below the Plan view. It provides one-click access to common programming tasks that are contextual to the file currently open in the editor.

Instead of navigating to a chat panel or writing a detailed prompt, the developer clicks a button. The button either:
1. **Inserts a structured `[wf]` TODO** into the active file (picked up by the TodoWatcher and queued as an instruction), or
2. **Opens a guided wizard** (multi-step input boxes) that builds a richer, structured instruction before queuing it.

This keeps the developer in their flow. The AI sees the intent as a properly formed instruction attached to the right file and line.

---

## Panel Layout

```
┌─────────────────────────────────┐
│  WATERFREE                   │
│                                 │
│  ▼ Plan                         │
│    ✦ Task 1 — Add validation    │
│    ○ Task 2 — Write tests       │
│                                 │
│  ▼ Quick Actions          [.py] │
│    ⚡ Clean up this file        │
│    📄 Document this file        │
│    🧪 Build test suite          │
│    🔍 Find bugs & code smells   │
│    ❓ Explain this file         │
│    💥 WTF happened here         │
│    🔒 Security audit            │
└─────────────────────────────────┘
```

The `[.py]` badge updates to reflect the active file's language. Actions that don't apply to the current language are hidden or greyed out.

---

## Actions by File Type

### Python (`.py`)

| Action | Label | Behaviour |
|---|---|---|
| Clean up | "Clean up this file" | Inserts cleanup TODO — style, dead code, readability |
| Document | "Document this file" | Wizard: choose docstring style (NumPy/Google/Sphinx) → inserts detailed TODO |
| Test suite | "Build test suite" | BDD Wizard (see below) |
| Find bugs | "Find bugs & code smells" | Inserts audit TODO |
| Explain | "Explain this file" | Inserts explain TODO, opens output channel for response |
| WTF mode | "WTF happened here" | Inserts blame/audit TODO |
| Security | "Security audit" | Inserts security-focused review TODO |

### TypeScript / JavaScript (`.ts`, `.tsx`, `.js`, `.jsx`)

| Action | Label | Behaviour |
|---|---|---|
| Clean up | "Clean up this file" | Inserts cleanup TODO |
| Document | "Add JSDoc comments" | Wizard: full-file or selection-only → inserts TODO |
| Test suite | "Build test suite" | BDD Wizard (Jest/Vitest style) |
| Type safety | "Review type safety" | Inserts type-checking TODO — finds `any`, unsafe casts |
| Find bugs | "Find bugs & code smells" | Inserts audit TODO |
| Explain | "Explain this file" | Inserts explain TODO |
| WTF mode | "WTF happened here" | Inserts blame/audit TODO |

### C# (`.cs`)

| Action | Label | Behaviour |
|---|---|---|
| Clean up | "Clean up this file" | Inserts cleanup TODO |
| Document | "Add XML doc comments" | Inserts documentation TODO |
| Test suite | "Build test suite" | BDD Wizard (xUnit/NUnit style) |
| Find bugs | "Find bugs & code smells" | Inserts audit TODO |
| SOLID check | "Review SOLID principles" | Inserts design-review TODO |

### Any file (always visible)

| Action | Label | Behaviour |
|---|---|---|
| Explain | "Explain this file" | Inserts explain TODO |
| WTF mode | "WTF happened here" | Inserts blame/audit TODO |

---

## BDD Test Suite Wizard

The "Build test suite" button opens a guided wizard specific to the current language.

### Python Wizard Flow

```
Step 1 of 4: What function or class are you testing?
  Prompt: "e.g. UserAuthService.login or parse_config()"
  → target = "UserAuthService.login"

Step 2 of 4: Describe the happy path in plain English.
  Prompt: "e.g. Valid username and password returns a JWT token"
  → happy_path = "..."

Step 3 of 4: List edge cases (one per line, or leave blank).
  Prompt: "e.g. Empty password, expired account, SQL injection"
  → edge_cases = ["empty password", "expired account", "SQL injection"]

Step 4 of 4: Any testing framework preferences?
  Options: [pytest (Recommended), unittest, pytest-bdd, Other]
  → framework = "pytest-bdd"
```

**Generated TODO inserted at top of file:**
```python
# TODO: [wf] Build a BDD-style test suite for UserAuthService.login
# Happy path: Valid username and password returns a JWT token
# Edge cases:
#   - empty password → should raise AuthError with clear message
#   - expired account → should raise AccountExpiredError
#   - SQL injection in username → should sanitise and reject
# Framework: pytest-bdd
# Create test file at: tests/test_user_auth_service.py
# Use fixtures in conftest.py for database setup/teardown
```

### TypeScript/Jest Wizard Flow

```
Step 1 of 3: What function or class are you testing?
Step 2 of 3: Describe the happy path.
Step 3 of 3: Any edge cases to cover?
  (framework defaults to Jest/Vitest based on package.json detection)
```

**Generated TODO:**
```typescript
// TODO: [wf] Build a Jest test suite for UserAuthService.login
// Happy path: valid credentials return { token, expiresAt }
// Edge cases: empty password throws AuthError, invalid user returns 401
// Test file: src/__tests__/UserAuthService.test.ts
// Use beforeEach/afterEach for mock setup, jest.fn() for dependencies
```

---

## Documentation Wizard

### Python — "Document this file"

```
Step 1 of 2: Docstring style?
  Options: [NumPy (Recommended), Google, Sphinx/reStructuredText]

Step 2 of 2: Document which items?
  Multi-select: [All public functions, All classes, Module docstring, Private functions too]
```

**Generated TODO:**
```python
# TODO: [wf] Add NumPy-style docstrings to all public functions and classes.
# Include: Parameters, Returns, Raises, Examples sections.
# Do not modify existing logic.
```

### TypeScript — "Add JSDoc comments"

```
Step 1 of 1: Document which items?
  Multi-select: [All exported functions (Recommended), All classes, All interfaces, Private methods too]
```

---

## WTF Mode

No wizard. Inserts immediately:

```python
# TODO: [wf] Audit this file. I need to understand what's going wrong here.
# - What does this file do? Summarise in plain English.
# - What are the worst parts? Be blunt.
# - What should be fixed first?
# - Were there any obvious mistakes made?
```

This is the "code review from a harsh senior dev" instruction.

---

## Cleanup Action

No wizard. Inserts immediately:

```python
# TODO: [wf] Clean up this file.
# - Remove dead code and commented-out blocks
# - Fix naming to follow conventions (snake_case for Python)
# - Reduce duplication without over-abstracting
# - Ensure imports are clean and sorted
# - Do not change behaviour, only style and structure
```

---

## Security Audit Action

No wizard. Inserts immediately:

```python
# TODO: [wf] Security audit this file.
# - Check for injection vulnerabilities (SQL, command, path traversal)
# - Review input validation and sanitisation
# - Check for hardcoded secrets or credentials
# - Review authentication and authorisation logic
# - Flag any use of deprecated or insecure APIs
```

---

## Technical Implementation

### TypeScript — `QuickActionsProvider`

Implements `vscode.TreeDataProvider<QuickActionItem>`.

Subscribes to `vscode.window.onDidChangeActiveTextEditor` to detect file changes and refresh the tree.

```typescript
interface QuickActionItem {
  label: string;
  icon: string;             // codicon name
  command: string;          // vscode command id
  args?: unknown[];
  language?: string;        // only show for this language id
  description?: string;     // shown as secondary text
}
```

### Action Insertion

All actions that insert a TODO call a shared helper:

```typescript
async function insertPpTodo(editor: vscode.TextEditor, lines: string[]): Promise<void> {
  const commentChar = getCommentChar(editor.document.languageId);
  const comment = lines.map(l => `${commentChar} ${l}`).join('\n');
  await editor.edit(editBuilder => {
    editBuilder.insert(new vscode.Position(0, 0), comment + '\n\n');
  });
}
```

Comment characters by language:
- `#` — Python, Ruby, Shell, YAML, TOML
- `//` — TypeScript, JavaScript, C#, Java, Go, Rust, C/C++
- `--` — SQL, Lua

### Wizard Helper

Multi-step wizards use `vscode.window.showInputBox` in sequence (no Webview required):

```typescript
async function runWizard(steps: WizardStep[]): Promise<Record<string, string> | null> {
  const results: Record<string, string> = {};
  for (const step of steps) {
    const value = await vscode.window.showInputBox({
      title: step.title,
      prompt: step.prompt,
      placeHolder: step.placeholder,
      validateInput: step.required
        ? (v) => v.trim() ? null : 'Required'
        : undefined,
    });
    if (value === undefined) return null; // cancelled
    results[step.key] = value;
  }
  return results;
}
```

### Language Detection

```typescript
function getLanguageId(editor: vscode.TextEditor): string {
  return editor.document.languageId; // 'python', 'typescript', 'javascript', 'csharp', etc.
}
```

VS Code provides `languageId` on the document object — no file extension parsing needed.

---

## Backend Integration

Quick actions do **not** require a backend round-trip. They write a `[wf]` TODO comment into the file using the VS Code edit API. The TodoWatcher picks this up on the next file-save event and queues it as an instruction.

This means:
- Actions work offline / before the backend is ready
- The developer can see exactly what instruction was queued (it's in their file)
- The developer can edit the TODO before saving if they want to refine it

**Exception: "Explain this file"** — this action bypasses the TODO mechanism and calls the backend directly with the full file content, showing the response in a new output channel tab or information modal. This requires a new `explainFile` backend method.

---

## New Backend Method: `explainFile`

```
→ {"id": "x", "method": "explainFile", "params": {"file": "/path/to/file.py", "workspacePath": "/..."}}
← {"id": "x", "result": {"summary": "...", "keyComponents": [...], "mainPurpose": "..."}}
```

Claude is given the full file content and the index summary for context, then asked to explain the file in plain English. The response is shown in a dedicated output channel: "WaterFree: File Explanation".

---

## New Backend Method: `auditFile`

Called by "WTF happened here" and "Find bugs" when the developer wants a direct answer (not a queued TODO). Optional — the simple TODO-insert path is the default.

```
→ {"id": "x", "method": "auditFile", "params": {"file": "...", "mode": "wtf|bugs|security", "workspacePath": "..."}}
← {"id": "x", "result": {"findings": [...], "topIssue": "...", "verdict": "..."}}
```

---

## Relationship to Existing TODO System

Quick Actions are the high-level entry point to the same `[wf]` TODO pipeline that already exists:

```
Developer clicks "Build test suite"
  → Wizard runs
  → Structured [wf] TODO inserted at line 1
  → Developer saves file (Ctrl+S)
  → TodoWatcher detects new [wf] TODO
  → queueTodoInstruction called on backend
  → Task appears in Plan sidebar (if session active)
  → AI picks it up on next annotation cycle
```

If there is no active session, the TODO sits in the file harmlessly until a session is started — at which point the TodoWatcher will detect it on the next save.

---

## File: `src/ui/QuickActionsProvider.ts`

See implementation. This file is the only new TypeScript file required for v1 of this feature.

---

## Remaining Professional Quality Gaps

The following are known gaps to address before this tool can be considered a professional-grade pair programming assistant:

### P0 — Blockers

| Gap | Impact | Fix |
|---|---|---|
| No code execution after approval | approveAnnotation does nothing after marking approved | Add `executeTask` backend method + call it after approval |
| Debug "Add as Task" creates new session | Replaces current work | Call `queueTodoInstruction` on existing session instead |

### P1 — Core Quality

| Gap | Impact | Fix |
|---|---|---|
| Side effect scanner has no logic | SCANNING state unreachable in practice | Implement `RippleDetector` post-execution scan in backend |
| No backend request timeout | Hangs forever if Python crashes mid-call | Add 30s timeout in PythonBridge + auto-restart |
| Inline annotation decorations not rendering | CodeLens shows but gutter highlight missing | Fix DecorationRenderer to track active file |

### P2 — Polish

| Gap | Impact | Fix |
|---|---|---|
| No cost/token tracking | Blind to API spend | Add session token counter to session notes |
| No pre-edit file snapshots | No undo path after AI edits | Snapshot file before WorkspaceEdit, add "Revert last AI edit" |
| CodeLens line numbers shift on edits | Annotations point to wrong lines | Anchor by function name, not line number |
