# WaterFree MSI Installer

This folder contains the MSI installer build for WaterFree.

## Requirements

- .NET SDK 8.0+ (for the installer helper and WiX SDK)
- WiX Toolset v4 SDK (via NuGet restore)
- Python 3.10+ (only to build the backend exe)

## Build

```powershell
dotnet build .\installer\WaterFreeInstaller.wixproj -c Release
```

Outputs:

- `installer\out\WaterFreeSetup.msi`

## Notes

The installer is per-user and installs under:

- `%LocalAppData%\WaterFree\`

It configures MCP servers for Codex and Claude by editing:

- `~/.codex/config.toml`
- `~/.claude.json`

No CLI dependency is required.

## Build prerequisites

The MSI build expects these artifacts to exist:

- `bin\waterfree-win32-x64.exe` (backend runtime)
- `waterfree.vsix` (VS Code extension)

Build the backend exe (PyInstaller):

```powershell
python -m pip install -r .\backend\requirements.txt pyinstaller
python -m PyInstaller .\waterfree.spec --noconfirm --distpath .\bin
 
npm run vscode:prepublish
npx --yes @vscode/vsce package --no-dependencies --skip-license --allow-missing-repository --allow-star-activation --allow-package-all-secrets --out .\waterfree.vsix --baseContentUrl https://localhost
```
