# Subsystem 05 — Side Effect Watcher
## WaterFree VS Code Extension

---

## Purpose

This is what makes the AI feel like the "obsessive colleague who checks everything." After every code execution, and on every file save, this subsystem scans for ripple effects — things that could break, patterns that were violated, error messages that will appear, and dependencies that have shifted.

It does not just lint. It reasons about the change in context of the whole codebase.

---

## What It Checks

### 1. Ripple Effect Detection
Using the CodeGraph from Subsystem 01: what else in the codebase is affected by what was just changed?

### 2. Type Consistency
For TypeScript: are all callers of a modified function still passing compatible arguments? Does the return type change break any downstream assignments?

### 3. Style Guide Enforcement
Against the project's established patterns (inferred from the codebase, plus explicit rules).

### 4. Error Message Interpretation
When a file save triggers a TypeScript/ESLint diagnostic, the AI interprets what it actually means in context — not just the raw error text.

### 5. Test Coverage Awareness
Are there existing tests that cover the modified code? If so, do they need updating? If there are no tests for the modified function, flag it.

### 6. Pattern Consistency
Does the new code follow the same patterns as similar existing code? (e.g. error handling style, async patterns, naming conventions)

---

## Components

### RippleDetector.ts

```typescript
class RippleDetector {

  // Coord-precise scan: instead of scanning all functions in an edited file,
  // scan only the specific symbols named in the task's coords.
  async scan(completedTask: Task): Promise<RippleReport> {
    const affectedNodes: GraphNode[] = [];

    // Primary: scan the targetCoord symbol and anything it calls
    const primarySymbol = completedTask.targetCoord;
    const primaryCallers = codeGraph.getCallers(
      primarySymbol.method ?? '',
      primarySymbol.file
    );
    affectedNodes.push(...primaryCallers);

    // Secondary: scan any contextCoords marked as 'modify' (not read-only)
    for (const coord of completedTask.contextCoords ?? []) {
      if (coord.anchorType === 'modify' || coord.anchorType === 'delete') {
        const callers = codeGraph.getCallers(coord.method ?? '', coord.file);
        affectedNodes.push(...callers);
      }
    }

    const ranked = this.rankByImpact(affectedNodes, completedTask);

    return {
      taskId: completedTask.id,
      affectedFiles: [...new Set(ranked.map(n => n.filePath))],
      affectedFunctions: ranked,
      highPriorityWarnings: ranked.filter(n => n.impactScore > 0.7),
      scanTimestamp: Date.now(),
    };
  }

  // Verify the task actually touched what it was supposed to.
  // Compares git diff against the task's targetCoord line range.
  // If the diff does not intersect the coord, flag as potentially incomplete.
  async verifyTaskCoordWasTouched(task: Task): Promise<boolean> {
    const resolvedLine = await indexManager.resolveCoordLine(task.targetCoord);
    const symbolRange = await indexManager.getSymbolLineRange(task.targetCoord);
    const diff = await gitClient.getDiffLines(task.targetCoord.file);

    const touched = diff.some(line =>
      line >= symbolRange.start && line <= symbolRange.end
    );

    if (!touched) {
      sidebarProvider.flagTask(task.id, 'Task marked complete but target location was not modified');
    }

    return touched;
  }

  private rankByImpact(nodes: GraphNode[], task: Task): RankedNode[] {
    // Impact score based on:
    // - How directly the node calls the edited symbol (0.9 for direct callers)
    // - How critical the calling function is (entry points score higher)
    // - Whether the caller is tested (untested callers are higher risk)
  }
}

interface RippleReport {
  taskId: string;
  affectedFiles: string[];
  affectedFunctions: RankedNode[];
  highPriorityWarnings: RankedNode[];
  scanTimestamp: number;
}

interface RankedNode {
  functionName: string;
  filePath: string;
  startLine: number;
  relationship: 'direct_caller' | 'indirect_caller' | 'type_user' | 'importer';
  impactScore: number;        // 0-1
  riskReason: string;         // one sentence: why this is risky
  hasCoverage: boolean;       // whether there are tests for this
}
```

---

### StyleGuideChecker.ts

The style guide is not manually configured. It is inferred from the existing codebase.

**Style patterns inferred at index time:**
```typescript
interface InferredStyleGuide {
  // Error handling
  errorHandlingPattern: 'throw' | 'return-result' | 'callback' | 'mixed';
  errorBaseClass?: string;               // e.g. 'AppError', 'BaseException'
  
  // Async patterns
  asyncPattern: 'async-await' | 'promises' | 'callbacks' | 'mixed';
  
  // Naming conventions
  variableNaming: 'camelCase' | 'snake_case' | 'mixed';
  fileNaming: 'kebab-case' | 'PascalCase' | 'camelCase';
  classNaming: 'PascalCase' | 'other';
  
  // Function style
  preferArrowFunctions: boolean;
  preferFunctionDeclarations: boolean;
  
  // Import style
  importStyle: 'named' | 'default' | 'mixed';
  importOrder: string[];               // inferred ordering pattern
  
  // Type annotation density (TypeScript)
  typeAnnotationLevel: 'explicit' | 'inferred' | 'minimal';
  
  // Comment style
  docstringFormat: 'jsdoc' | 'none' | 'inline';
  
  // Test patterns (if tests present)
  testFramework?: 'jest' | 'vitest' | 'mocha' | 'pytest';
  testNamingPattern?: string;          // e.g. "should [verb] when [condition]"
}
```

**Style violation detection prompt:**
```
The following code was just written as part of a task:

[NEW CODE]

Here is the inferred style guide for this project:
[InferredStyleGuide as JSON]

Here are 3 examples of similar existing code in this project:
[EXAMPLE 1]
[EXAMPLE 2]
[EXAMPLE 3]

Identify any style inconsistencies between the new code and the project's established patterns.
Be specific and practical — only flag things that actually matter.
Do not flag valid alternatives if the project uses mixed approaches.

Respond with a JSON StyleViolation[] array. Empty array if no violations.
```

```typescript
interface StyleViolation {
  line: number;
  severity: 'error' | 'warning' | 'suggestion';
  category: string;          // e.g. "error-handling", "naming", "async-pattern"
  description: string;       // what's wrong
  suggestion: string;        // how to fix it
  example?: string;          // a line from the codebase showing the preferred pattern
}
```

---

### ErrorInterpreter.ts

VS Code provides raw diagnostic messages from TypeScript, ESLint, etc. These are often cryptic. The ErrorInterpreter translates them into useful, context-aware explanations.

```typescript
class ErrorInterpreter {
  
  // Subscribes to vscode.languages.onDidChangeDiagnostics
  async onDiagnosticsChanged(uri: vscode.Uri, diagnostics: vscode.Diagnostic[]): Promise<void> {
    // Filter to diagnostics in recently edited files only
    const relevantDiagnostics = diagnostics.filter(d => 
      recentlyEditedFiles.has(uri.fsPath)
    );
    
    if (relevantDiagnostics.length === 0) return;
    
    for (const diagnostic of relevantDiagnostics) {
      const interpretation = await this.interpret(uri, diagnostic);
      diagnosticRenderer.showInterpretation(uri, diagnostic, interpretation);
    }
  }
  
  async interpret(uri: vscode.Uri, diagnostic: vscode.Diagnostic): Promise<DiagnosticInterpretation> {
    const fileContent = await readFileAroundLine(uri, diagnostic.range.start.line, 10);
    const functionContext = indexManager.getFunctionAtLine(uri.fsPath, diagnostic.range.start.line);
    
    const prompt = `
      A TypeScript/ESLint error appeared after a recent code change.
      
      Error: "${diagnostic.message}" (code: ${diagnostic.code})
      Location: ${uri.fsPath}:${diagnostic.range.start.line}
      
      Code at that location:
      ${fileContent}
      
      Function context: ${JSON.stringify(functionContext)}
      
      Recent change that caused this: ${recentChangeSummary}
      
      Explain in plain English:
      1. What this error actually means
      2. Why the recent change caused it
      3. The most likely fix (one sentence)
      4. Whether this is blocking or can be deferred
      
      Be concise. This appears inline next to the error.
    `;
    
    return await claudeClient.interpret(prompt);
  }
}

interface DiagnosticInterpretation {
  plainExplanation: string;   // e.g. "processPayment now returns void but CheckoutController expects a boolean"
  causedBy: string;           // link back to the change that caused it
  likelyFix: string;          // one-sentence fix
  isBlocking: boolean;        // will this prevent compilation/running?
  autoFixable: boolean;       // can PairProtocol fix this automatically?
}
```

**Rendering:** The interpretation appears as a CodeLens line directly below the error squiggle — not a hover tooltip (too easy to miss) and not a panel (too heavy). The developer sees the error squiggle, glances down one line, and reads the plain-English explanation.

---

### TestCoverageAnalyser.ts

After each task completes, check whether the modified code is tested.

```typescript
class TestCoverageAnalyser {
  
  async analyseTask(task: Task, editedFiles: string[]): Promise<CoverageReport> {
    const uncoveredFunctions: UncoveredFunction[] = [];
    
    for (const file of editedFiles) {
      const functions = parsedIndex.getFunctionsInFile(file);
      
      for (const fn of functions) {
        const testFile = this.findTestFile(file);
        const isCovered = testFile ? await this.isTestedIn(fn, testFile) : false;
        
        if (!isCovered) {
          uncoveredFunctions.push({
            functionName: fn.name,
            filePath: file,
            testFilePath: testFile ?? this.suggestTestFilePath(file),
            isNew: task.annotations.some(a => a.willCreate.includes(fn.name)),
          });
        }
      }
    }
    
    return { uncoveredFunctions, taskId: task.id };
  }
  
  private findTestFile(filePath: string): string | null {
    // Looks for __tests__/filename.test.ts, filename.spec.ts, etc.
    // Based on inferred test file naming from IndexManager
  }
  
  private async isTestedIn(fn: FunctionDef, testFile: string): Promise<boolean> {
    // Checks if the function name appears in the test file's call sites
    const testParsed = await treeParser.parseFile(testFile);
    return testParsed.callSites.some(cs => cs.calleeFunction === fn.name);
  }
}
```

---

### SideEffectWatcher.ts — Orchestrator

```typescript
class SideEffectWatcher {
  
  // Called after every task execution
  async scan(task: Task): Promise<SideEffectReport> {
    // Derive the edited file set from the task's coords (not just annotation metadata)
    const editedFiles = [
      task.targetCoord.file,
      ...(task.contextCoords ?? [])
        .filter(c => c.anchorType !== 'read-only-context')
        .map(c => c.file),
      ...task.annotations.flatMap(a => a.willCreate),
    ];
    const uniqueFiles = [...new Set(editedFiles)];

    // Verify the primary coord was actually touched before scanning
    await rippleDetector.verifyTaskCoordWasTouched(task);

    const [ripple, style, coverage] = await Promise.all([
      rippleDetector.scan(task),
      styleGuideChecker.checkFiles(uniqueFiles),
      testCoverageAnalyser.analyseTask(task, uniqueFiles),
    ]);
    
    const report: SideEffectReport = {
      taskId: task.id,
      ripple,
      styleViolations: style,
      coverageGaps: coverage,
      severity: this.calculateOverallSeverity(ripple, style, coverage),
    };
    
    await this.renderReport(report);
    return report;
  }
  
  // Called on every file save (for active session files only)
  async onFileSave(filePath: string): Promise<void> {
    if (!session.isActiveFile(filePath)) return;
    
    // Quick scan only — no AI involved, just graph + TS diagnostics
    const quickRipple = await rippleDetector.quickScan(filePath);
    
    if (quickRipple.highPriorityWarnings.length > 0) {
      sidebarProvider.showRippleWarnings(quickRipple.highPriorityWarnings);
    }
  }
  
  private async renderReport(report: SideEffectReport): Promise<void> {
    // 1. Update task in sidebar with warning count
    // 2. For high severity: show notification with "View Details"
    // 3. Render CodeLens annotations on affected functions in other files
    // 4. Add warnings to the completed task's record in session.json
    
    if (report.severity === 'high') {
      const choice = await vscode.window.showWarningMessage(
        `Side effect scan: ${report.ripple.highPriorityWarnings.length} high-priority impacts detected`,
        'Review Now',
        'See Summary',
        'Dismiss'
      );
      
      if (choice === 'Review Now') {
        navigationManager.navigateToFirstWarning(report);
      }
    }
  }
}
```

---

## Side Effect Report UI

**In the sidebar**, completed tasks show a badge:
```
✅ 2. Create RateLimiter middleware  ⚠️ 2
```

Clicking the badge opens the side effect details panel.

**Side effect details panel:**
```
┌─ Side Effects: Task 2 ───────────────────────────────────────┐
│                                                               │
│ RIPPLE EFFECTS (2)                                            │
│ ─────────────────                                             │
│ ⚠️  RetryQueue.retryFailed()             HIGH                 │
│    Direct caller. If it retries from the same IP, it will     │
│    be rate-limited. Consider a bypass flag.                   │
│    → src/queues/RetryQueue.ts:203  [Navigate]                 │
│                                                               │
│ ○  AuthController.handleLogin()         LOW                   │
│    Indirect caller via middleware stack. No immediate issue.  │
│    → src/auth/AuthController.ts:45  [Navigate]               │
│                                                               │
│ STYLE (1)                                                     │
│ ──────                                                        │
│ ○  RateLimiter.ts:34 — Error thrown directly; project        │
│    pattern is return Result<T, E>. Consider aligning.         │
│    Example: src/payment/PaymentGateway.ts:167  [View]         │
│                                                               │
│ TEST COVERAGE (1)                                             │
│ ──────────────                                                │
│ ○  RateLimiter.checkLimit() — no test coverage               │
│    Suggested: __tests__/middleware/RateLimiter.test.ts         │
│    [Generate test stub]                                       │
│                                                               │
│ [Add all warnings as tasks]  [Dismiss all]                    │
└───────────────────────────────────────────────────────────────┘
```

**"Add all warnings as tasks"** converts each warning into a new Task in the plan queue — the obsessive colleague's flag becomes an actionable item the human can schedule.

---

## Configuration

```json
{
  "waterfree.sideEffects.scanOnSave": {
    "type": "boolean",
    "default": true
  },
  "waterfree.sideEffects.rippleDepth": {
    "type": "number",
    "default": 3,
    "description": "How many hops through the call graph to scan"
  },
  "waterfree.sideEffects.notifyOnSeverity": {
    "type": "string",
    "enum": ["all", "high", "none"],
    "default": "high"
  },
  "waterfree.sideEffects.checkStyle": {
    "type": "boolean",
    "default": true
  },
  "waterfree.sideEffects.checkCoverage": {
    "type": "boolean",
    "default": true
  }
}
```
