# WaterFree MSI Installer

This folder contains the MSI installer build for WaterFree.

## Requirements

- WiX Toolset v4 (`wix` on PATH)
- .NET SDK 8.0+ (for the installer helper)
- Python 3.10+ (only if you need to build the backend exe)

## Build

```powershell
.\installer\build-installer.ps1
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
