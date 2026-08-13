/**
 * Mirrors the VS Code settings that the standalone `waterfree` CLI needs into
 * `.waterfree/config.json`.
 *
 * The CLI is invoked directly from a shell by coding agents, so it never sees
 * the extension host's configuration. Anything an agent-facing command must
 * respect has to be written somewhere on disk that the Python side can read.
 * Keep this list small — only settings the CLI genuinely consumes belong here.
 */

import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

/** Settings mirrored to disk, as `settingKey -> configJsonKey`. */
const MIRRORED_SETTINGS: ReadonlyArray<readonly [string, string]> = [
  ["godotPath", "godotPath"],
];

const CONFIG_DIR = ".waterfree";
const CONFIG_FILE = "config.json";

/**
 * Write the mirrored settings to `{workspaceRoot}/.waterfree/config.json`,
 * preserving any keys written by other tooling.
 *
 * Best-effort: a read-only or otherwise unwritable workspace must not break
 * activation, so failures are reported to the caller rather than thrown.
 */
export function syncWorkspaceConfig(workspaceRoot: string): Error | null {
  const configDir = path.join(workspaceRoot, CONFIG_DIR);
  const configPath = path.join(configDir, CONFIG_FILE);
  const settings = vscode.workspace.getConfiguration("waterfree");

  let existing: Record<string, unknown> = {};
  try {
    if (fs.existsSync(configPath)) {
      const parsed: unknown = JSON.parse(fs.readFileSync(configPath, "utf8"));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        existing = parsed as Record<string, unknown>;
      }
    }
  } catch {
    // Corrupt or unreadable config — rewrite it from scratch rather than
    // leaving the CLI reading a file we can no longer reason about.
    existing = {};
  }

  const next = { ...existing };
  for (const [settingKey, configKey] of MIRRORED_SETTINGS) {
    const value = settings.get<string>(settingKey, "").trim();
    if (value) {
      next[configKey] = value;
    } else {
      delete next[configKey];
    }
  }

  try {
    fs.mkdirSync(configDir, { recursive: true });
    fs.writeFileSync(configPath, `${JSON.stringify(next, null, 2)}\n`, "utf8");
    return null;
  } catch (err) {
    return err instanceof Error ? err : new Error(String(err));
  }
}

/** Keep the mirror current as the user edits settings. */
export function watchWorkspaceConfig(workspaceRoot: string): vscode.Disposable {
  return vscode.workspace.onDidChangeConfiguration((event) => {
    const touched = MIRRORED_SETTINGS.some(([settingKey]) =>
      event.affectsConfiguration(`waterfree.${settingKey}`),
    );
    if (touched) {
      syncWorkspaceConfig(workspaceRoot);
    }
  });
}
