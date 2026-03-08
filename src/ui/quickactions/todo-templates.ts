/**
 * TODO comment templates for Quick Actions.
 */

import { toSnakeCase, toPascalCase } from "../../utils/strings.js";

export const TODOS = {
  cleanup: (lang: string): string[] => {
    const style = lang === "python" ? "snake_case, PEP 8" : lang === "csharp" ? "PascalCase, C# conventions" : "camelCase, standard conventions";
    return [
      "TODO: [wf] Clean up this file.",
      `- Remove dead code, commented-out blocks, and unused imports`,
      `- Fix naming to follow ${style}`,
      `- Reduce duplication without premature abstraction`,
      `- Do NOT change behaviour — style and structure only`,
    ];
  },

  findBugs: (): string[] => [
    "TODO: [wf] Find bugs and code smells in this file.",
    "- Identify logic errors and incorrect assumptions",
    "- Flag anti-patterns and over-engineered sections",
    "- Note any paths that could crash or produce incorrect output",
    "- List the 3 most important issues in priority order",
  ],

  wtf: (): string[] => [
    "TODO: [wf] Audit this file. Be blunt.",
    "- What does this file actually do? (plain English, 2 sentences)",
    "- What are the worst parts? Name them specifically.",
    "- What should be fixed or rewritten first, and why?",
    "- Were there any obvious mistakes made here?",
  ],

  explain: (): string[] => [
    "TODO: [wf] Explain this file.",
    "- What is the purpose of this file?",
    "- What are the main components (classes, functions, exports)?",
    "- What does a caller need to know to use this correctly?",
    "- Are there any non-obvious behaviours or gotchas?",
  ],

  typeSafety: (): string[] => [
    "TODO: [wf] Review this file for type safety.",
    "- Find all uses of `any` and suggest specific types",
    "- Identify unsafe type assertions (`as SomeType`)",
    "- Flag missing null/undefined checks at boundaries",
    "- Note any functions with implicit return types that should be explicit",
  ],

  solid: (): string[] => [
    "TODO: [wf] Review this file against SOLID principles.",
    "- SRP: Does each class have a single, clear responsibility?",
    "- OCP: Is it open for extension without modification?",
    "- LSP: Do subclasses honour the contracts of their base types?",
    "- ISP: Are interfaces focused, or do they force unnecessary dependencies?",
    "- DIP: Does high-level code depend on abstractions, not concrete types?",
  ],

  security: (): string[] => [
    "TODO: [wf] Security audit this file.",
    "- Check for injection vulnerabilities (SQL, command, path traversal, template)",
    "- Review input validation and sanitisation at every entry point",
    "- Look for hardcoded secrets, credentials, or API keys",
    "- Check authentication and authorisation logic for bypass paths",
    "- Flag any use of deprecated or known-insecure APIs",
  ],

  documentPython: (style: string, scope: string): string[] => [
    `TODO: [wf] Add ${style}-style docstrings to this file.`,
    `Scope: ${scope}`,
    `Include all relevant sections: Parameters, Returns, Raises, Examples`,
    `Do not modify any existing logic — documentation only`,
  ],

  documentJs: (scope: string): string[] => [
    "TODO: [wf] Add JSDoc comments to this file.",
    `Scope: ${scope}`,
    "Include @param, @returns, @throws tags with types",
    "Do not modify any existing logic — documentation only",
  ],

  testSuite: (
    target: string,
    happyPath: string,
    edgeCases: string[],
    framework: string,
    lang: string,
  ): string[] => {
    const testFile = lang === "python"
      ? `tests/test_${toSnakeCase(target)}.py`
      : `src/__tests__/${toPascalCase(target)}.test.ts`;

    const lines = [
      `TODO: [wf] Build a BDD-style test suite for ${target}.`,
      `Happy path: ${happyPath}`,
    ];

    if (edgeCases.length > 0) {
      lines.push("Edge cases:");
      edgeCases.forEach((ec) => lines.push(`  - ${ec}`));
    }

    lines.push(`Framework: ${framework}`);
    lines.push(`Test file: ${testFile}`);

    if (lang === "python") {
      lines.push("Use fixtures in conftest.py for shared setup and teardown");
    } else {
      lines.push("Use beforeEach/afterEach for setup, jest.fn() or vi.fn() for mocks");
    }

    return lines;
  },
};
