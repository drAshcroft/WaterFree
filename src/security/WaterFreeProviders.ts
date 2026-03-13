import * as path from "path";
import * as vscode from "vscode";

export type ProviderType = "claude" | "openai" | "groq" | "ollama" | "huggingface" | "mock";
export type ProviderConnectionStyle = "native" | "compatible" | "local" | "none";
export type ProviderStage =
  | "planning"
  | "annotation"
  | "execution"
  | "debug"
  | "question_answer"
  | "ripple_detection"
  | "alter_annotation"
  | "knowledge";
export type ProviderReloadMode = "manual" | "on_change";
export const DEFAULT_PROVIDER_STAGES: ProviderStage[] = [
  "planning",
  "annotation",
  "execution",
  "debug",
  "question_answer",
  "ripple_detection",
  "alter_annotation",
  "knowledge",
];

export interface ProviderConfig {
  id: string;
  type: ProviderType;
  name: string;
  baseUrl: string;
  models: string[];
  modes?: string[];
  useWith?: string;
  enabled: boolean;
  connectionStyle?: ProviderConnectionStyle;
}

export interface ProviderStatus extends ProviderConfig {
  maskedKey: string;
  hasKey: boolean;
  hasConnection: boolean;
  connectionLabel: string;
}

export interface ProviderProfileConnection {
  style: ProviderConnectionStyle;
  baseUrl: string;
  secretRef: string;
}

export interface ProviderProfileFeatures {
  tools: boolean;
  skills: boolean;
  checkpoints: boolean;
  subagents: boolean;
  summarization: boolean;
}

export interface ProviderProfileRouting {
  useForStages: ProviderStage[];
  personas: string[];
}

export interface OpenAIProviderOptimizations {
  useResponsesApi: boolean;
  usePreviousResponseId: boolean;
  promptCacheKeyStrategy: string;
  promptCacheRetention: string | null;
  streamUsage: boolean;
}

export interface AnthropicProviderOptimizations {
  enablePromptCaching: boolean;
}

export interface ProviderProfileEntry {
  id: string;
  type: ProviderType;
  enabled: boolean;
  label: string;
  connection: ProviderProfileConnection;
  models: Record<string, string>;
  features: ProviderProfileFeatures;
  optimizations: {
    openai?: OpenAIProviderOptimizations;
    anthropic?: AnthropicProviderOptimizations;
  };
  routing: ProviderProfileRouting;
}

export interface ProviderPersonaAssignment {
  personaId: string;
  providerId: string;
  model: string;
  stages: ProviderStage[];
}

export interface ProviderProfilePolicies {
  fallbackProviderOrder: string[];
  sessionKeyStrategy: string;
  flushOnTaskComplete: boolean;
  flushOnProviderSwitch: boolean;
  reloadMode: ProviderReloadMode;
  personaAssignments: ProviderPersonaAssignment[];
  personaPromptOverrides: Record<string, string>;
  summarizationThresholds: Partial<Record<string, number>>;
}

export interface ProviderPersonaCustomization {
  personaId: string;
  prompt: string;
  assignments: Array<Omit<ProviderPersonaAssignment, "personaId">>;
}

export interface ProviderProfileDocument {
  version: number;
  activeProviderId: string;
  catalog: ProviderProfileEntry[];
  policies: ProviderProfilePolicies;
}

export interface BackendProviderProfileConnection extends ProviderProfileConnection {
  apiKey: string;
}

export interface BackendProviderProfileEntry extends Omit<ProviderProfileEntry, "connection"> {
  connection: BackendProviderProfileConnection;
}

export interface BackendProviderProfileDocument extends Omit<ProviderProfileDocument, "catalog"> {
  catalog: BackendProviderProfileEntry[];
}

const PROVIDERS_KEY = "waterfree.providers.v1";
const PROVIDERS_FILENAME = "providers.json";
const DEFAULT_PROVIDER_URLS: Partial<Record<ProviderType, string>> = {
  claude: "https://api.anthropic.com",
  openai: "https://api.openai.com/v1",
  groq: "https://api.groq.com/openai/v1",
  ollama: "http://localhost:11434",
  huggingface: "https://router.huggingface.co/v1",
};
const DEFAULT_MODELS: Record<Exclude<ProviderType, "mock">, string[]> = {
  claude: ["claude-opus-4-6", "claude-sonnet-4-6"],
  openai: ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"],
  groq: ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
  ollama: ["llama3.2"],
  huggingface: [],
};
const DEFAULT_STAGE_MODELS: Record<Exclude<ProviderType, "mock">, Record<string, string>> = {
  claude: {
    default: "claude-sonnet-4-6",
    planning: "claude-sonnet-4-6",
    annotation: "claude-sonnet-4-6",
    execution: "claude-sonnet-4-6",
    debug: "claude-sonnet-4-6",
  },
  openai: {
    default: "o3-mini",
    planning: "o3-mini",
    annotation: "gpt-4o-mini",
    execution: "gpt-4o",
    debug: "gpt-4o-mini",
  },
  groq: {
    default: "llama-3.3-70b-versatile",
    planning: "llama-3.3-70b-versatile",
    annotation: "llama-3.1-8b-instant",
    execution: "llama-3.3-70b-versatile",
    debug: "llama-3.1-8b-instant",
  },
  ollama: {
    default: "llama3.2",
    planning: "llama3.2",
    annotation: "llama3.2",
    execution: "llama3.2",
    debug: "llama3.2",
  },
  huggingface: {
    default: "",
    planning: "",
    annotation: "",
    execution: "",
    debug: "",
  },
};
const DEFAULT_POLICIES: ProviderProfilePolicies = {
  fallbackProviderOrder: [],
  sessionKeyStrategy: "workspace_stage_persona_provider",
  flushOnTaskComplete: true,
  flushOnProviderSwitch: true,
  reloadMode: "on_change",
  personaAssignments: [],
  personaPromptOverrides: {},
  summarizationThresholds: {
    EXECUTION: 60000,
    PLANNING: 30000,
    ANNOTATION: 30000,
    ALTER_ANNOTATION: 30000,
    LIVE_DEBUG: 20000,
    RIPPLE_DETECTION: 15000,
    QUESTION_ANSWER: 15000,
  },
};

export class WaterFreeProviders {
  private _profile: ProviderProfileDocument = emptyProfile();

  constructor(
    private readonly _context: vscode.ExtensionContext,
    private readonly _workspacePath: string,
  ) {}

  async initialize(legacyKey?: string): Promise<void> {
    const fileProfile = await this._loadFileProfile();
    if (fileProfile) {
      this._profile = fileProfile;
      await this._saveProfileIfChanged(fileProfile);
      return;
    }

    const stored = this._context.globalState.get<unknown[]>(PROVIDERS_KEY);
    const migrated = migrateLegacyProviders(stored, legacyKey);
    this._profile = normalizeProfileDocument(migrated);
    await this._persistProfile();
  }

  async getProfile(): Promise<ProviderProfileDocument> {
    return cloneProfile(this._profile);
  }

  async setProfile(profile: ProviderProfileDocument): Promise<void> {
    this._profile = normalizeProfileDocument(profile);
    await this._persistProfile();
  }

  async reloadProfile(): Promise<ProviderProfileDocument> {
    const raw = await this._readProfileFile();
    this._profile = normalizeProfileDocument(raw ?? this._profile);
    return cloneProfile(this._profile);
  }

  watch(onDidChange: (profile: ProviderProfileDocument) => void): vscode.Disposable {
    const pattern = new vscode.RelativePattern(
      this._workspacePath,
      path.posix.join(".waterfree", PROVIDERS_FILENAME),
    );
    const watcher = vscode.workspace.createFileSystemWatcher(pattern, false, false, false);
    const refresh = async (): Promise<void> => {
      try {
        const profile = await this.reloadProfile();
        onDidChange(profile);
      } catch {
        // Ignore malformed edits until the next valid save.
      }
    };
    watcher.onDidCreate(() => { void refresh(); });
    watcher.onDidChange(() => { void refresh(); });
    watcher.onDidDelete(() => {
      void this.reloadProfile().then(onDidChange).catch(() => undefined);
    });
    return watcher;
  }

  async exportBackendConfig(): Promise<BackendProviderProfileDocument> {
    const catalog = await Promise.all(this._profile.catalog.map(async (entry) => ({
      ...entry,
      connection: {
        ...entry.connection,
        apiKey: acceptsApiKey(entry)
          ? await this._getKey(entry.id)
          : "",
      },
    })));
    return {
      ...cloneProfile(this._profile),
      catalog,
    };
  }

  async getStatuses(): Promise<ProviderStatus[]> {
    return Promise.all(this._profile.catalog.map(async (entry) => {
      const key = acceptsApiKey(entry) ? await this._getKey(entry.id) : "";
      const connection = describeConnection(entry, key);
      const models = uniqueModels(entry.models);
      return {
        id: entry.id,
        type: entry.type,
        name: entry.label,
        baseUrl: entry.connection.baseUrl,
        models,
        modes: routingToModes(entry.routing),
        useWith: routingToUseWith(entry.routing),
        enabled: entry.enabled,
        connectionStyle: entry.connection.style,
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
    for (const entry of this._profile.catalog) {
      if (!entry.enabled) { continue; }
      if (allowedTypes && !allowedTypes.has(entry.type)) { continue; }
      if (!acceptsApiKey(entry)) { continue; }
      const key = await this._getKey(entry.id);
      if (key) { return key; }
    }
    return "";
  }

  async add(config: Omit<ProviderConfig, "id">, apiKey: string): Promise<void> {
    const id = `provider-${Date.now()}`;
    const entry = legacyConfigToProfileEntry({ ...config, id });
    if (!entry) { return; }
    this._profile.catalog.push(entry);
    if (!this._profile.activeProviderId) {
      this._profile.activeProviderId = entry.id;
    }
    if (apiKey && acceptsApiKey(entry)) {
      await this._setKey(id, apiKey);
    }
    this._profile = normalizeProfileDocument(this._profile);
    await this._persistProfile();
  }

  async update(id: string, updates: Partial<Omit<ProviderConfig, "id">>, apiKey: string): Promise<void> {
    const idx = this._profile.catalog.findIndex((entry) => entry.id === id);
    if (idx < 0) { return; }
    const mergedLegacy = {
      ...profileEntryToLegacyConfig(this._profile.catalog[idx]),
      ...updates,
      id,
    };
    const next = legacyConfigToProfileEntry(mergedLegacy);
    if (!next) { return; }
    this._profile.catalog[idx] = next;
    if (!acceptsApiKey(next)) {
      await this._deleteKey(id);
    } else if (apiKey) {
      await this._setKey(id, apiKey);
    }
    this._profile = normalizeProfileDocument(this._profile);
    await this._persistProfile();
  }

  async updatePersonaAssignments(personaId: string, assignments: Array<Omit<ProviderPersonaAssignment, "personaId">>): Promise<void> {
    const normalizedPersonaId = String(personaId || "").trim().toLowerCase();
    const kept = this._profile.policies.personaAssignments.filter((entry) => entry.personaId !== normalizedPersonaId);
    this._profile = normalizeProfileDocument({
      ...this._profile,
      policies: {
        ...this._profile.policies,
        personaAssignments: [
          ...kept,
          ...assignments.map((entry) => ({
            ...entry,
            personaId: normalizedPersonaId,
          })),
        ],
      },
    });
    await this._persistProfile();
  }

  async setPersonaCustomizations(customizations: ProviderPersonaCustomization[]): Promise<void> {
    const personaAssignments: ProviderPersonaAssignment[] = [];
    const personaPromptOverrides: Record<string, string> = {};
    for (const item of customizations) {
      const personaId = String(item.personaId || "").trim().toLowerCase();
      if (!personaId) { continue; }
      const prompt = String(item.prompt || "").trim();
      if (prompt) {
        personaPromptOverrides[personaId] = prompt;
      }
      for (const assignment of item.assignments || []) {
        personaAssignments.push({
          personaId,
          providerId: assignment.providerId,
          model: assignment.model || "",
          stages: Array.isArray(assignment.stages) ? assignment.stages.slice() : [],
        });
      }
    }
    this._profile = normalizeProfileDocument({
      ...this._profile,
      policies: {
        ...this._profile.policies,
        personaAssignments,
        personaPromptOverrides,
      },
    });
    await this._persistProfile();
  }

  async toggle(id: string): Promise<void> {
    const entry = this._profile.catalog.find((item) => item.id === id);
    if (!entry) { return; }
    entry.enabled = !entry.enabled;
    this._profile = normalizeProfileDocument(this._profile);
    await this._persistProfile();
  }

  async remove(id: string): Promise<void> {
    this._profile.catalog = this._profile.catalog.filter((entry) => entry.id !== id);
    if (this._profile.activeProviderId === id) {
      this._profile.activeProviderId = this._profile.catalog[0]?.id ?? "";
    }
    await this._deleteKey(id);
    this._profile = normalizeProfileDocument(this._profile);
    await this._persistProfile();
  }

  private async _loadFileProfile(): Promise<ProviderProfileDocument | null> {
    const raw = await this._readProfileFile();
    if (!raw) {
      return null;
    }
    return normalizeProfileDocument(raw);
  }

  private async _readProfileFile(): Promise<unknown | null> {
    try {
      const raw = await vscode.workspace.fs.readFile(this._profileUri());
      return JSON.parse(Buffer.from(raw).toString("utf-8"));
    } catch {
      return null;
    }
  }

  private async _saveProfileIfChanged(profile: ProviderProfileDocument): Promise<void> {
    const normalized = normalizeProfileDocument(profile);
    const raw = JSON.stringify(normalized, null, 2) + "\n";
    try {
      const current = await vscode.workspace.fs.readFile(this._profileUri());
      if (Buffer.from(current).toString("utf-8") === raw) {
        return;
      }
    } catch {
      // Fall through to create file.
    }
    await this._writeProfile(normalized);
  }

  private async _persistProfile(): Promise<void> {
    await this._writeProfile(this._profile);
    const legacyMirror = this._profile.catalog.map(profileEntryToLegacyConfig);
    await this._context.globalState.update(PROVIDERS_KEY, legacyMirror);
  }

  private async _writeProfile(profile: ProviderProfileDocument): Promise<void> {
    const dir = vscode.Uri.joinPath(vscode.Uri.file(this._workspacePath), ".waterfree");
    await vscode.workspace.fs.createDirectory(dir);
    const raw = JSON.stringify(normalizeProfileDocument(profile), null, 2) + "\n";
    await vscode.workspace.fs.writeFile(this._profileUri(), Buffer.from(raw, "utf-8"));
  }

  private _profileUri(): vscode.Uri {
    return vscode.Uri.joinPath(
      vscode.Uri.file(this._workspacePath),
      ".waterfree",
      PROVIDERS_FILENAME,
    );
  }

  private async _getKey(id: string): Promise<string> {
    return (await this._context.secrets.get(secretRefForProvider(id))) ?? "";
  }

  private async _setKey(id: string, key: string): Promise<void> {
    await this._context.secrets.store(secretRefForProvider(id), key);
  }

  private async _deleteKey(id: string): Promise<void> {
    await this._context.secrets.delete(secretRefForProvider(id));
  }
}

function emptyProfile(): ProviderProfileDocument {
  return normalizeProfileDocument({
    version: 1,
    activeProviderId: "",
    catalog: [],
    policies: DEFAULT_POLICIES,
  });
}

function normalizeProfileDocument(raw: unknown): ProviderProfileDocument {
  const source = isRecord(raw) ? raw : {};
  const rawCatalog = Array.isArray(source.catalog)
    ? source.catalog
    : Array.isArray(source.providers)
      ? source.providers
      : [];
  const catalog = rawCatalog
    .map((entry, index) => normalizeProfileEntry(entry, `provider-${index + 1}`))
    .filter((entry): entry is ProviderProfileEntry => entry !== null);
  const fallbackProviderOrder = Array.isArray(source.policies) ? [] : normalizeFallbackOrder(source, catalog);
  const activeProviderId = pickActiveProviderId(
    typeof source.activeProviderId === "string" ? source.activeProviderId.trim() : "",
    catalog,
  );
  return {
    version: 1,
    activeProviderId,
    catalog,
    policies: {
      ...DEFAULT_POLICIES,
      ...(isRecord(source.policies) ? {
        fallbackProviderOrder,
        sessionKeyStrategy: normalizeString(source.policies.sessionKeyStrategy, DEFAULT_POLICIES.sessionKeyStrategy),
        flushOnTaskComplete: source.policies.flushOnTaskComplete !== false,
        flushOnProviderSwitch: source.policies.flushOnProviderSwitch !== false,
        reloadMode: source.policies.reloadMode === "manual" ? "manual" : "on_change",
        personaAssignments: normalizePersonaAssignments(source.policies.personaAssignments, catalog),
        personaPromptOverrides: normalizePersonaPromptOverrides(source.policies.personaPromptOverrides),
        summarizationThresholds: normalizeSummarizationThresholds(source.policies.summarizationThresholds),
      } : {
        fallbackProviderOrder,
        personaAssignments: normalizePersonaAssignments(undefined, catalog),
        personaPromptOverrides: normalizePersonaPromptOverrides(undefined),
      }),
    },
  };
}

function normalizeProfileEntry(raw: unknown, fallbackId: string): ProviderProfileEntry | null {
  if (!isRecord(raw)) {
    return null;
  }
  const type = normalizeProviderType(raw.type);
  if (!type) {
    return null;
  }
  const id = normalizeString(raw.id, fallbackId);
  const connectionBaseUrl = isRecord(raw.connection) ? raw.connection.baseUrl : raw.baseUrl;
  const baseUrl = normalizeBaseUrl(type, connectionBaseUrl);
  const style = isRecord(raw.connection)
    ? normalizeConnectionStyle(type, raw.connection.style, baseUrl)
    : normalizeConnectionStyle(type, raw.connectionStyle, baseUrl);
  const label = normalizeString(raw.label, normalizeName(raw.name, type));
  const models = normalizeStageModels(type, raw.models);
  const routing = normalizeRouting(raw.routing, raw.modes, raw.useWith);
  const features = normalizeFeatures(raw.features);
  const optimizations = normalizeOptimizations(type, raw.optimizations);
  return {
    id,
    type,
    enabled: raw.enabled !== false,
    label,
    connection: {
      style,
      baseUrl,
      secretRef: isRecord(raw.connection)
        ? normalizeString(raw.connection.secretRef, secretRefForProvider(id))
        : secretRefForProvider(id),
    },
    models,
    features,
    optimizations,
    routing,
  };
}

function normalizeStageModels(
  type: ProviderType,
  value: unknown,
): Record<string, string> {
  const defaults = type === "mock" ? { default: "" } : DEFAULT_STAGE_MODELS[type];
  if (isRecord(value)) {
    const next: Record<string, string> = {};
    for (const [key, entry] of Object.entries(value)) {
      const normalized = String(entry ?? "").trim();
      if (normalized) {
        next[key] = normalized;
      }
    }
    if (!next.default) {
      next.default = next.planning || defaults.default || "";
    }
    return { ...defaults, ...next };
  }
  const list = normalizeModels(type, value);
  if (list.length > 0 && list[0]) {
    return {
      ...defaults,
      default: list[0],
      planning: list[0],
      annotation: list[1] || list[0],
      execution: list[0],
      debug: list[1] || list[0],
    };
  }
  return { ...defaults };
}

function normalizeRouting(
  rawRouting: unknown,
  legacyModes?: unknown,
  legacyUseWith?: unknown,
): ProviderProfileRouting {
  const legacyStages = normalizeModes(legacyModes).map(modeToStage);
  if (!isRecord(rawRouting)) {
    return {
      useForStages: legacyStages.length > 0 ? legacyStages : [...DEFAULT_PROVIDER_STAGES],
      personas: normalizeUseWith(legacyUseWith),
    };
  }
  const stages = Array.isArray(rawRouting.useForStages)
    ? rawRouting.useForStages
      .map((entry) => normalizeStage(entry))
      .filter((entry): entry is ProviderStage => entry !== null)
    : legacyStages;
  return {
    useForStages: stages.length > 0 ? stages : [...DEFAULT_PROVIDER_STAGES],
    personas: Array.isArray(rawRouting.personas)
      ? rawRouting.personas.map((entry) => String(entry).trim()).filter(Boolean)
      : normalizeUseWith(legacyUseWith),
  };
}

function normalizePersonaAssignments(
  raw: unknown,
  catalog: ProviderProfileEntry[],
): ProviderPersonaAssignment[] {
  const validIds = new Set(catalog.map((entry) => entry.id));
  if (Array.isArray(raw)) {
    return raw
      .map((entry) => normalizePersonaAssignment(entry, validIds))
      .filter((entry): entry is ProviderPersonaAssignment => entry !== null);
  }

  const migrated: ProviderPersonaAssignment[] = [];
  for (const entry of catalog) {
    if (!entry.routing.personas.length) {
      continue;
    }
    for (const personaId of entry.routing.personas) {
      migrated.push({
        personaId: personaId.trim().toLowerCase(),
        providerId: entry.id,
        model: entry.models.default || "",
        stages: entry.routing.useForStages.length > 0
          ? [...entry.routing.useForStages]
          : [...DEFAULT_PROVIDER_STAGES],
      });
    }
  }
  return migrated;
}

function normalizePersonaAssignment(
  raw: unknown,
  validIds: Set<string>,
): ProviderPersonaAssignment | null {
  if (!isRecord(raw)) {
    return null;
  }
  const personaId = String(raw.personaId ?? "").trim().toLowerCase();
  const providerId = String(raw.providerId ?? "").trim();
  if (!personaId || !providerId || !validIds.has(providerId)) {
    return null;
  }
  const stages = Array.isArray(raw.stages)
    ? raw.stages
      .map((entry) => normalizeStage(entry))
      .filter((entry): entry is ProviderStage => entry !== null)
    : [];
  return {
    personaId,
    providerId,
    model: String(raw.model ?? "").trim(),
    stages: stages.length > 0 ? Array.from(new Set(stages)) : [...DEFAULT_PROVIDER_STAGES],
  };
}

function normalizePersonaPromptOverrides(raw: unknown): Record<string, string> {
  if (!isRecord(raw)) {
    return {};
  }
  const normalized: Record<string, string> = {};
  for (const [key, value] of Object.entries(raw)) {
    const personaId = String(key || "").trim().toLowerCase();
    const prompt = String(value || "").trim();
    if (personaId && prompt) {
      normalized[personaId] = prompt;
    }
  }
  return normalized;
}

function normalizeFeatures(raw: unknown): ProviderProfileFeatures {
  const source = isRecord(raw) ? raw : {};
  return {
    tools: source.tools !== false,
    skills: source.skills !== false,
    checkpoints: source.checkpoints !== false,
    subagents: source.subagents !== false,
    summarization: source.summarization !== false,
  };
}

function normalizeOptimizations(
  type: ProviderType,
  raw: unknown,
): ProviderProfileEntry["optimizations"] {
  const source = isRecord(raw) ? raw : {};
  const optimizations: ProviderProfileEntry["optimizations"] = {};
  if (type === "openai") {
    const openai = isRecord(source.openai) ? source.openai : {};
    optimizations.openai = {
      useResponsesApi: openai.useResponsesApi !== false,
      usePreviousResponseId: openai.usePreviousResponseId !== false,
      promptCacheKeyStrategy: normalizeString(openai.promptCacheKeyStrategy, "session_stage_persona"),
      promptCacheRetention: openai.promptCacheRetention == null
        ? null
        : String(openai.promptCacheRetention).trim() || null,
      streamUsage: openai.streamUsage !== false,
    };
  }
  if (type === "claude") {
    const anthropic = isRecord(source.anthropic) ? source.anthropic : {};
    optimizations.anthropic = {
      enablePromptCaching: anthropic.enablePromptCaching !== false,
    };
  }
  return optimizations;
}

function normalizeFallbackOrder(
  raw: Record<string, unknown>,
  catalog: ProviderProfileEntry[],
): string[] {
  const policies = isRecord(raw.policies) ? raw.policies : {};
  const configured = Array.isArray(policies.fallbackProviderOrder)
    ? policies.fallbackProviderOrder.map((entry) => String(entry).trim()).filter(Boolean)
    : [];
  const known = new Set(catalog.map((entry) => entry.id));
  const deduped = Array.from(new Set(configured.filter((entry) => known.has(entry))));
  for (const entry of catalog) {
    if (!deduped.includes(entry.id)) {
      deduped.push(entry.id);
    }
  }
  return deduped;
}

function normalizeSummarizationThresholds(raw: unknown): Partial<Record<string, number>> {
  if (!isRecord(raw)) {
    return { ...DEFAULT_POLICIES.summarizationThresholds };
  }
  const thresholds: Partial<Record<string, number>> = {};
  for (const [key, value] of Object.entries(raw)) {
    const numberValue = Number(value);
    if (Number.isFinite(numberValue) && numberValue > 0) {
      thresholds[key.toUpperCase()] = numberValue;
    }
  }
  return Object.keys(thresholds).length > 0 ? thresholds : { ...DEFAULT_POLICIES.summarizationThresholds };
}

function pickActiveProviderId(activeProviderId: string, catalog: ProviderProfileEntry[]): string {
  if (activeProviderId && catalog.some((entry) => entry.id === activeProviderId)) {
    return activeProviderId;
  }
  return catalog.find((entry) => entry.enabled)?.id ?? catalog[0]?.id ?? "";
}

function migrateLegacyProviders(
  stored: unknown[] | undefined,
  legacyKey?: string,
): ProviderProfileDocument {
  const catalog = (stored ?? [])
    .map((entry, index) => legacyConfigToProfileEntry(entry, `provider-${index + 1}`))
    .filter((entry): entry is ProviderProfileEntry => entry !== null);
  if (catalog.length === 0 && legacyKey) {
    catalog.push(legacyConfigToProfileEntry({
      id: "provider-anthropic-default",
      type: "claude",
      name: "Claude (Anthropic)",
      baseUrl: "",
      models: ["claude-opus-4-6", "claude-sonnet-4-6"],
      modes: ["planning", "execution"],
      useWith: "all",
      enabled: true,
      connectionStyle: "native",
    }, "provider-anthropic-default")!);
  }
  return normalizeProfileDocument({
    version: 1,
    activeProviderId: catalog[0]?.id ?? "",
    catalog,
    policies: DEFAULT_POLICIES,
  });
}

function legacyConfigToProfileEntry(raw: unknown, fallbackId?: string): ProviderProfileEntry | null {
  if (!isRecord(raw)) {
    return null;
  }
  const type = normalizeProviderType(raw.type);
  if (!type) {
    return null;
  }
  const id = normalizeString(raw.id, fallbackId ?? "provider");
  return normalizeProfileEntry({
    id,
    type,
    label: raw.name,
    enabled: raw.enabled,
    baseUrl: raw.baseUrl,
    connectionStyle: raw.connectionStyle,
    models: raw.models,
    modes: raw.modes,
    useWith: raw.useWith,
  }, id);
}

function profileEntryToLegacyConfig(entry: ProviderProfileEntry): ProviderConfig {
  return {
    id: entry.id,
    type: entry.type,
    name: entry.label,
    baseUrl: entry.connection.baseUrl,
    models: uniqueModels(entry.models),
    modes: routingToModes(entry.routing),
    useWith: routingToUseWith(entry.routing),
    enabled: entry.enabled,
    connectionStyle: entry.connection.style,
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
    case "groq":
      return "groq";
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
    case "groq":
      return "Groq";
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
  if (url) {
    return url;
  }
  return DEFAULT_PROVIDER_URLS[type] || "";
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
    default:
      break;
  }
  if (baseUrl) {
    return isLocalUrl(baseUrl) ? "local" : "compatible";
  }
  return "native";
}

function acceptsApiKey(provider: Pick<ProviderProfileEntry, "type" | "connection">): boolean {
  if (provider.type === "mock" || provider.type === "ollama") {
    return false;
  }
  return provider.connection.style !== "none";
}

function describeConnection(provider: ProviderProfileEntry, key: string): {
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
      maskedKey: provider.connection.baseUrl,
      hasCredential: true,
      ready: Boolean(provider.connection.baseUrl),
      label: "local",
    };
  }
  if (provider.type === "openai") {
    if (provider.connection.style === "native") {
      return {
        maskedKey: maskKey(key),
        hasCredential: Boolean(key),
        ready: Boolean(key),
        label: "chatgpt",
      };
    }
    const connectionValue = key ? maskKey(key) : provider.connection.baseUrl;
    return {
      maskedKey: connectionValue,
      hasCredential: Boolean(key) || Boolean(provider.connection.baseUrl),
      ready: Boolean(provider.connection.baseUrl),
      label: provider.connection.style ?? "compatible",
    };
  }
  return {
    maskedKey: maskKey(key),
    hasCredential: Boolean(key),
    ready: Boolean(key),
    label: provider.connection.style ?? "native",
  };
}

function modeToStage(mode: string): ProviderStage {
  switch (mode) {
    case "planning":
      return "planning";
    case "execution":
      return "execution";
    case "indexing":
      return "knowledge";
    default:
      return "planning";
  }
}

function normalizeStage(value: unknown): ProviderStage | null {
  const normalized = String(value ?? "").trim().toLowerCase();
  switch (normalized) {
    case "planning":
    case "annotation":
    case "execution":
    case "debug":
    case "question_answer":
    case "ripple_detection":
    case "alter_annotation":
    case "knowledge":
      return normalized;
    default:
      return null;
  }
}

function normalizeUseWith(value: unknown): string[] {
  const normalized = String(value ?? "").trim();
  if (!normalized || normalized === "all") {
    return [];
  }
  return [normalized];
}

function routingToModes(routing: ProviderProfileRouting): string[] {
  const modes = new Set<string>();
  for (const stage of routing.useForStages) {
    if (stage === "knowledge") {
      modes.add("indexing");
    } else if (stage === "execution") {
      modes.add("execution");
    } else {
      modes.add("planning");
    }
  }
  return Array.from(modes);
}

function routingToUseWith(routing: ProviderProfileRouting): string {
  return routing.personas[0] ?? "all";
}

function uniqueModels(models: Record<string, string>): string[] {
  return Array.from(new Set(Object.values(models).filter(Boolean)));
}

function secretRefForProvider(id: string): string {
  return `waterfree.provider.${id}.key`;
}

function normalizeString(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function isLocalUrl(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  return normalized.includes("localhost")
    || normalized.includes("127.0.0.1")
    || normalized.includes("0.0.0.0");
}

function isRecord(value: unknown): value is Record<string, any> {
  return typeof value === "object" && value !== null;
}

function maskKey(key: string): string {
  if (!key) { return ""; }
  if (key.length <= 14) { return "•".repeat(key.length); }
  return key.slice(0, 10) + "…" + key.slice(-4);
}

function cloneProfile(profile: ProviderProfileDocument): ProviderProfileDocument {
  return JSON.parse(JSON.stringify(profile)) as ProviderProfileDocument;
}
