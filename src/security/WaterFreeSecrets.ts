import { spawnSync } from "child_process";
import * as path from "path";
import * as vscode from "vscode";

const ANTHROPIC_SECRET_KEY = "waterfree.anthropicApiKey";
const INSTALLER_PROMPT_STATE_KEY = "waterfree.setupPromptShown";
const MOCK_MODE_STATE_KEY = "waterfree.mockMode";

export class WaterFreeSecrets {
  private _anthropicApiKey = "";
  private _isMock = false;

  constructor(
    private readonly _context: vscode.ExtensionContext,
    private readonly _extensionPath: string,
  ) {}

  async initialize(): Promise<void> {
    this._anthropicApiKey = await this._loadAnthropicApiKey();
    this._isMock = this._context.globalState.get<boolean>(MOCK_MODE_STATE_KEY, false);
  }

  get anthropicApiKey(): string {
    return this._isMock ? "" : this._anthropicApiKey;
  }

  get isMock(): boolean {
    return this._isMock;
  }

  get hasRealKey(): boolean {
    return Boolean(this._anthropicApiKey);
  }

  /** Raw stored key regardless of mock mode — for provider migration */
  get storedKey(): string {
    return this._anthropicApiKey;
  }

  getMaskedKey(): string {
    if (!this._anthropicApiKey) { return ""; }
    const k = this._anthropicApiKey;
    if (k.length <= 14) { return "•".repeat(k.length); }
    return k.slice(0, 10) + "…" + k.slice(-4);
  }

  async removeAnthropicApiKey(): Promise<void> {
    await this._context.secrets.delete(ANTHROPIC_SECRET_KEY);
    this._anthropicApiKey = "";
    this._isMock = false;
    await this._context.globalState.update(MOCK_MODE_STATE_KEY, false);
  }

  async setMockMode(enabled: boolean): Promise<void> {
    this._isMock = enabled;
    await this._context.globalState.update(MOCK_MODE_STATE_KEY, enabled);
  }

  async promptForSetupIfNeeded(): Promise<boolean> {
    if (this._anthropicApiKey) {
      return false;
    }

    const alreadyPrompted = this._context.globalState.get<boolean>(INSTALLER_PROMPT_STATE_KEY, false);
    if (alreadyPrompted) {
      return false;
    }

    await this._context.globalState.update(INSTALLER_PROMPT_STATE_KEY, true);
    const choice = await vscode.window.showInformationMessage(
      "WaterFree needs an Anthropic API key. Store it securely now?",
      "Set API Key",
      "Later",
    );
    if (choice !== "Set API Key") {
      return false;
    }

    return this.promptForAnthropicApiKey();
  }

  async promptForAnthropicApiKey(): Promise<boolean> {
    const apiKey = await vscode.window.showInputBox({
      prompt: "Enter your Anthropic API key",
      placeHolder: "sk-ant-...",
      password: true,
      ignoreFocusOut: true,
      validateInput: (value) => (value.trim() ? null : "Enter an API key."),
    });
    if (!apiKey) {
      return false;
    }

    await this.setAnthropicApiKey(apiKey.trim());
    return true;
  }

  async setAnthropicApiKey(apiKey: string): Promise<void> {
    const trimmed = apiKey.trim();
    await this._context.secrets.store(ANTHROPIC_SECRET_KEY, trimmed);
    this._anthropicApiKey = trimmed;
  }

  private async _loadAnthropicApiKey(): Promise<string> {
    const stored = (await this._context.secrets.get(ANTHROPIC_SECRET_KEY))?.trim();
    if (stored) {
      return stored;
    }

    const migrated = await this._migrateLegacySetting();
    if (migrated) {
      return migrated;
    }

    const imported = this._readInstallerSecret("ANTHROPIC_API_KEY");
    if (imported) {
      await this._context.secrets.store(ANTHROPIC_SECRET_KEY, imported);
      return imported;
    }

    return "";
  }

  private async _migrateLegacySetting(): Promise<string> {
    const config = vscode.workspace.getConfiguration("waterfree");
    const legacy = config.get<string>("anthropicApiKey")?.trim() ?? "";
    if (!legacy) {
      return "";
    }

    await this._context.secrets.store(ANTHROPIC_SECRET_KEY, legacy);
    await config.update("anthropicApiKey", "", vscode.ConfigurationTarget.Global);
    return legacy;
  }

  private _readInstallerSecret(name: string): string {
    const scriptPath = path.join(this._extensionPath, "scripts", "waterfree-secrets.ps1");
    const result = spawnSync(
      "powershell",
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        scriptPath,
        "-Action",
        "Read",
        "-Name",
        name,
      ],
      {
        cwd: this._extensionPath,
        encoding: "utf-8",
        windowsHide: true,
      },
    );

    if (result.status !== 0) {
      return "";
    }

    return result.stdout.trim();
  }
}
