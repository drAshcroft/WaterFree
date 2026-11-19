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

### VS Code extension

The helper installs the bundled `waterfree.vsix` with the VS Code CLI. A
failure there does not roll back the MSI, so the helper records the outcome in
`%LocalAppData%\WaterFree\logs\installer\vscode-extension-status.json`
(`installed`, `reload-required`, `restart-required`, `failed` or `skipped`,
with what to do next) and logs a `[WARN] VS Code extension ...` line to
`%TEMP%\WaterFreeInstaller-helper.log`. `build_installer.ps1 -Deploy` prints
that status and repeats it at the end of the build.

The VSIX is packaged with the MSI's ProductVersion (`build_installer.ps1`
passes it to `vsce package --no-update-package-json`), so each build is a real
extension upgrade. `package.json` keeps its checked-in base version.

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
