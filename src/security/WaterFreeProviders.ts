import * as vscode from "vscode";

export type ProviderType = "claude" | "openai" | "ollama" | "huggingface" | "mock";
export type ProviderConnectionStyle = "native" | "compatible" | "local" | "none";

export interface ProviderConfig {
  id: string;
  type: ProviderType;
  name: string;
  baseUrl: string;
  models: string[];
  modes: string[];
  useWith: string;
  enabled: boolean;
  connectionStyle?: ProviderConnectionStyle;
}

export interface ProviderStatus extends ProviderConfig {
  maskedKey: string;
  hasKey: boolean;
  hasConnection: boolean;
  connectionLabel: string;
}

const PROVIDERS_KEY = "waterfree.providers.v1";
const DEFAULT_MODELS: Record<Exclude<ProviderType, "mock">, string[]> = {
  claude: ["claude-opus-4-6", "claude-sonnet-4-6"],
  openai: ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"],
  ollama: ["llama3.2"],
  huggingface: [],
};

export class WaterFreeProviders {
  private _providers: ProviderConfig[] = [];

  constructor(private readonly _context: vscode.ExtensionContext) {}

  async initialize(legacyKey?: string): Promise<void> {
    const stored = this._context.globalState.get<unknown[]>(PROVIDERS_KEY);
    if (stored && stored.length > 0) {
      const normalized = stored
        .map((entry, index) => normalizeProviderConfig(entry, `provider-${index + 1}`))
        .filter((entry): entry is ProviderConfig => entry !== null);
      this._providers = normalized;
      if (JSON.stringify(stored) !== JSON.stringify(normalized)) {
        await this._save();
      }
      return;
    }
    if (legacyKey) {
      const id = "provider-anthropic-default";
      this._providers = [{
        id,
        type: "claude",
        name: "Claude (Anthropic)",
        baseUrl: "",
        models: ["claude-opus-4-6", "claude-sonnet-4-6"],
        modes: ["planning", "execution"],
        useWith: "all",
        enabled: true,
        connectionStyle: "native",
      }];
      await this._context.secrets.store(`waterfree.provider.${id}.key`, legacyKey);
      await this._save();
    }
  }

  async getStatuses(): Promise<ProviderStatus[]> {
    return Promise.all(this._providers.map(async (p) => {
      const key = (p.type === "mock" || p.type === "ollama")
        ? ""
        : await this._getKey(p.id);
      const connection = describeConnection(p, key);
      return {
        ...p,
        maskedKey: connection.maskedKey,
        hasKey: connection.hasCredential,
        hasConnection: connection.ready,
        connectionLabel: connection.label,
      };
    }));
  }

  async getPrimaryKey(preferredTypes?: ProviderType | ProviderType[]): Promise<string> {
    const allowedTypes = preferredTypes === undefined
      ? null
      : new Set(Array.isArray(preferredTypes) ? preferredTypes : [preferredTypes]);
    for (const p of this._providers) {
      if (!p.enabled || !p.modes.includes("planning")) { continue; }
      if (allowedTypes && !allowedTypes.has(p.type)) { continue; }
      if (p.type === "mock" || p.type === "ollama") { return ""; }
      const key = await this._getKey(p.id);
      if (key) { return key; }
    }
    return "";
  }

  async add(config: Omit<ProviderConfig, "id">, apiKey: string): Promise<void> {
    const id = `provider-${Date.now()}`;
    const normalized = normalizeProviderConfig({ ...config, id }, id);
    if (!normalized) { return; }
    this._providers.push(normalized);
    if (apiKey && acceptsApiKey(normalized)) {
      await this._setKey(id, apiKey);
    }
    await this._save();
  }

  async update(id: string, updates: Partial<Omit<ProviderConfig, "id">>, apiKey: string): Promise<void> {
    const idx = this._providers.findIndex((p) => p.id === id);
    if (idx < 0) { return; }
    const normalized = normalizeProviderConfig({ ...this._providers[idx], ...updates, id }, id);
    if (!normalized) { return; }
    this._providers[idx] = normalized;
    if (!acceptsApiKey(normalized)) {
      await this._deleteKey(id);
    } else if (apiKey) {
      await this._setKey(id, apiKey);
    }
    await this._save();
  }

  async toggle(id: string): Promise<void> {
    const idx = this._providers.findIndex((p) => p.id === id);
    if (idx < 0) { return; }
    this._providers[idx] = { ...this._providers[idx], enabled: !this._providers[idx].enabled };
    await this._save();
  }

  async remove(id: string): Promise<void> {
    this._providers = this._providers.filter((p) => p.id !== id);
    await this._deleteKey(id);
    await this._save();
  }

  private async _getKey(id: string): Promise<string> {
    return (await this._context.secrets.get(`waterfree.provider.${id}.key`)) ?? "";
  }

  private async _setKey(id: string, key: string): Promise<void> {
    await this._context.secrets.store(`waterfree.provider.${id}.key`, key);
  }

  private async _deleteKey(id: string): Promise<void> {
    await this._context.secrets.delete(`waterfree.provider.${id}.key`);
  }

  private async _save(): Promise<void> {
    await this._context.globalState.update(PROVIDERS_KEY, this._providers);
  }
}

function normalizeProviderConfig(raw: unknown, fallbackId: string): ProviderConfig | null {
  if (!isRecord(raw)) {
    return null;
  }
  const type = normalizeProviderType(raw.type);
  if (!type) {
    return null;
  }
  const baseUrl = normalizeBaseUrl(type, raw.baseUrl);
  const connectionStyle = normalizeConnectionStyle(type, raw.connectionStyle, baseUrl);
  const models = normalizeModels(type, raw.models);
  const name = normalizeName(raw.name, type);
  const modes = normalizeModes(raw.modes);
  const useWith = typeof raw.useWith === "string" && raw.useWith.trim()
    ? raw.useWith.trim()
    : "all";

  return {
    id: typeof raw.id === "string" && raw.id.trim() ? raw.id : fallbackId,
    type,
    name,
    baseUrl,
    models,
    modes,
    useWith,
    enabled: raw.enabled !== false,
    connectionStyle,
  };
}

function normalizeProviderType(value: unknown): ProviderType | null {
  const normalized = String(value ?? "").trim().toLowerCase();
  switch (normalized) {
    case "claude":
    case "anthropic":
      return "claude";
    case "openai":
    case "chatgpt":
    case "codex":
      return "openai";
    case "ollama":
      return "ollama";
    case "huggingface":
    case "hf":
      return "huggingface";
    case "mock":
      return "mock";
    default:
      return null;
  }
}

function normalizeName(value: unknown, type: ProviderType): string {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  switch (type) {
    case "claude":
      return "Claude";
    case "openai":
      return "OpenAI / ChatGPT";
    case "ollama":
      return "Ollama";
    case "huggingface":
      return "Hugging Face";
    case "mock":
      return "Mock";
  }
}

function normalizeBaseUrl(type: ProviderType, value: unknown): string {
  const url = typeof value === "string" ? value.trim().replace(/\/+$/, "") : "";
  if (type === "ollama") {
    return url || "http://localhost:11434";
  }
  return url;
}

function normalizeModels(type: ProviderType, value: unknown): string[] {
  const rawValues = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(",")
      : [];
  const models = Array.from(new Set(rawValues
    .map((entry) => String(entry).trim())
    .filter(Boolean)));

  if (models.length > 0) {
    return models;
  }
  if (type === "mock") {
    return [];
  }
  return [...DEFAULT_MODELS[type]];
}

function normalizeModes(value: unknown): string[] {
  const rawValues = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(",")
      : [];
  const modes = Array.from(new Set(rawValues
    .map((entry) => String(entry).trim())
    .filter((entry) => entry === "planning" || entry === "execution" || entry === "indexing")));
  return modes.length > 0 ? modes : ["planning", "execution"];
}

function normalizeConnectionStyle(
  type: ProviderType,
  value: unknown,
  baseUrl: string,
): ProviderConnectionStyle {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (type === "mock") {
    return "none";
  }
  if (type === "ollama") {
    return "local";
  }
  switch (normalized) {
    case "none":
      return "none";
    case "native":
    case "direct":
    case "official":
    case "chatgpt":
      return "native";
    case "compatible":
    case "openai-compatible":
    case "custom":
    case "proxy":
      return "compatible";
    case "local":
    case "localhost":
    case "self-hosted":
      return "local";
  }
  if (baseUrl) {
    return isLocalUrl(baseUrl) ? "local" : "compatible";
  }
  return "native";
}

function acceptsApiKey(provider: ProviderConfig): boolean {
  if (provider.type === "mock" || provider.type === "ollama") {
    return false;
  }
  return provider.connectionStyle !== "none";
}

function describeConnection(provider: ProviderConfig, key: string): {
  maskedKey: string;
  hasCredential: boolean;
  ready: boolean;
  label: string;
} {
  if (provider.type === "mock") {
    return {
      maskedKey: "",
      hasCredential: true,
      ready: true,
      label: "mock",
    };
  }
  if (provider.type === "ollama") {
    return {
      maskedKey: provider.baseUrl,
      hasCredential: true,
      ready: Boolean(provider.baseUrl),
      label: "local",
    };
  }
  if (provider.type === "openai") {
    if (provider.connectionStyle === "native") {
      return {
        maskedKey: maskKey(key),
        hasCredential: Boolean(key),
        ready: Boolean(key),
        label: "chatgpt",
      };
    }
    const connectionValue = key ? maskKey(key) : provider.baseUrl;
    return {
      maskedKey: connectionValue,
      hasCredential: Boolean(key) || Boolean(provider.baseUrl),
      ready: Boolean(provider.baseUrl),
      label: provider.connectionStyle ?? "compatible",
    };
  }
  return {
    maskedKey: maskKey(key),
    hasCredential: Boolean(key),
    ready: Boolean(key),
    label: provider.connectionStyle ?? "native",
  };
}

function isLocalUrl(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  return normalized.includes("localhost")
    || normalized.includes("127.0.0.1")
    || normalized.includes("0.0.0.0");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function maskKey(key: string): string {
  if (!key) { return ""; }
  if (key.length <= 14) { return "•".repeat(key.length); }
  return key.slice(0, 10) + "…" + key.slice(-4);
}
