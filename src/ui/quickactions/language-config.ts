/**
 * Comment character mapping by VS Code languageId.
 */

export const COMMENT_CHARS: Record<string, string> = {
  python: "#",
  ruby: "#",
  shellscript: "#",
  yaml: "#",
  toml: "#",
  r: "#",
  perl: "#",
  coffeescript: "#",
  typescript: "//",
  typescriptreact: "//",
  javascript: "//",
  javascriptreact: "//",
  csharp: "//",
  java: "//",
  go: "//",
  rust: "//",
  c: "//",
  cpp: "//",
  swift: "//",
  kotlin: "//",
  sql: "--",
  lua: "--",
};

export function commentChar(languageId: string): string {
  return COMMENT_CHARS[languageId] ?? "//";
}
