# WaterFree MSI Installer

This folder contains the MSI installer build for WaterFree.

## Requirements

- .NET SDK 8.0+ (for the installer helper and WiX SDK)
- WiX Toolset v4 SDK (via NuGet restore)
- Python 3.10+ (only to build the backend runtime)

## Build

```powershell
dotnet build .\installer\WaterFreeInstaller.wixproj -c Release
```

Outputs:

- `installer\out\WaterFreeSetup.msi`

## Notes

The installer is per-user and installs under:

- `%LocalAppData%\WaterFree\`

It does not register MCP servers. WaterFree installs `waterfree.exe` on the
user PATH and installs CLI-oriented skill packages for Codex and Claude:

- `~/.codex/skills`
- `~/.claude/skills`

During install/uninstall it also removes legacy WaterFree MCP entries from
Claude/Codex config files so stale registrations do not keep launching.

## Build prerequisites

The MSI build expects these artifacts to exist:

- `bin\waterfree-runtime.zip` (backend runtime)
- `waterfree.vsix` (VS Code extension)

Build the backend runtime (PyInstaller):

```powershell
python -m pip install -r .\backend\requirements.txt pyinstaller
.\build_installer.ps1 -SkipVsix -SkipMsi
 
npm run vscode:prepublish
npx --yes @vscode/vsce package --no-dependencies --skip-license --allow-missing-repository --allow-star-activation --allow-package-all-secrets --out .\waterfree.vsix --baseContentUrl https://localhost
```

The MSI also stages `skills\*\SKILL.md` from the repo and the installer helper
copies those packages into the user skill directories.
