import * as path from "path";
import * as vscode from "vscode";

export type WizardDocContext = {
  runId: string;
  stageId: string;
  wizardId: string;
  title: string;
};

export type WizardChunkMarker = {
  id: string;
  title: string;
  required: boolean;
  accepted: boolean;
  line: number;
};

const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---/;
const CHUNK_RE = /^##\s+(.+?)\r?\n<!-- wf:chunk (\{.+\}) -->/gm;

export function isWizardDoc(document: vscode.TextDocument): boolean {
  if (document.languageId !== "markdown") {
    return false;
  }
  return Boolean(parseWizardDocContext(document.getText()));
}

export function isWizardDocPath(docPath: string): boolean {
  return docPath.replace(/\\/g, "/").includes("/.waterfree/wizards/");
}

export function parseWizardDocContext(text: string): WizardDocContext | null {
  const frontmatter = FRONTMATTER_RE.exec(text);
  if (!frontmatter) {
    return null;
  }

  const values: Record<string, string> = {};
  for (const line of frontmatter[1].split(/\r?\n/)) {
    const idx = line.indexOf(":");
    if (idx <= 0) {
      continue;
    }
    const key = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim();
    values[key] = value;
  }

  if (values.waterfreeWizard !== "true" || !values.runId || !values.stageId || !values.wizardId) {
    return null;
  }

  return {
    runId: values.runId,
    stageId: values.stageId,
    wizardId: values.wizardId,
    title: values.title ?? values.stageId,
  };
}

export function parseWizardDocContextFromDocument(document: vscode.TextDocument): WizardDocContext | null {
  return parseWizardDocContext(document.getText());
}

export function parseChunkMarkers(document: vscode.TextDocument): WizardChunkMarker[] {
  const text = document.getText();
  const markers: WizardChunkMarker[] = [];
  for (const match of text.matchAll(CHUNK_RE)) {
    const chunkTitle = match[1];
    const raw = match[2];
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      continue;
    }
    const offset = match.index ?? 0;
    const line = document.positionAt(offset).line;
    markers.push({
      id: String(payload.id ?? ""),
      title: String(payload.title ?? chunkTitle),
      required: Boolean(payload.required),
      accepted: Boolean(payload.accepted),
      line,
    });
  }
  return markers.filter((marker) => marker.id.length > 0);
}

export function describeWizardDoc(document: vscode.TextDocument): string {
  const normalized = document.uri.fsPath.replace(/\\/g, "/");
  const idx = normalized.indexOf("/.waterfree/wizards/");
  if (idx < 0) {
    return path.basename(document.uri.fsPath);
  }
  return normalized.slice(idx + 1);
}
