<#
.SYNOPSIS
    Deploy the WaterFree VS Code extension and/or MCP skills/servers.

.DESCRIPTION
    Targets:
      local  - Build VSIX and install to VS Code (default)
      claude - Install skills + register MCP servers in Claude Code
      codex  - Install skills + register MCP servers in Codex
      all    - local + claude + codex

.PARAMETER Target
    Deployment target: local, claude, codex, all. Default: local

.PARAMETER Quick
    For 'local' target: compile without minification (faster iteration).

.PARAMETER PythonPath
    Python executable to use for MCP server commands. Default: python

.EXAMPLE
    .\deploy.ps1                    # build + install to VS Code (minified)
    .\deploy.ps1 -Quick             # build + install to VS Code (fast, no minify)
    .\deploy.ps1 claude             # install skills + MCP servers for Claude Code
    .\deploy.ps1 codex              # install skills + MCP servers for Codex
    .\deploy.ps1 all                # everything
    .\deploy.ps1 all -Quick         # everything, fast local build
    .\deploy.ps1 claude -PythonPath C:\Python\python.exe
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("local", "claude", "codex", "all")]
    [string]$Target = "local",

    [switch]$Quick,

    [string]$PythonPath = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir  = $PSScriptRoot
$SkillsDir  = Join-Path $ScriptDir "skills"
$VsixPath   = Join-Path $ScriptDir "waterfree.vsix"

$McpServers = @(
    @{ name = "waterfree-debug";     module = "backend.mcp_debug"     }
    @{ name = "waterfree-index";     module = "backend.mcp_index"     }
    @{ name = "waterfree-knowledge"; module = "backend.mcp_knowledge" }
    @{ name = "waterfree-todos";     module = "backend.mcp_todos"     }
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

function Write-Step([string]$msg) {
    Write-Host "`n==> $msg" -ForegroundColor Cyan
}

function Write-Ok([string]$msg) {
    Write-Host "    $msg" -ForegroundColor Green
}

# Merge WaterFree MCP servers into a JSON config file (Claude Code format).
function Merge-JsonMcpConfig([string]$ConfigPath) {
    $config = if (Test-Path $ConfigPath) {
        Get-Content $ConfigPath -Raw | ConvertFrom-Json
    } else {
        [PSCustomObject]@{}
    }

    if (-not ($config | Get-Member -Name mcpServers -MemberType NoteProperty)) {
        $config | Add-Member -Name mcpServers -MemberType NoteProperty -Value ([PSCustomObject]@{})
    }

    foreach ($s in $McpServers) {
        $entry = [PSCustomObject]@{
            command = $PythonPath
            args    = @("-m", $s.module)
            cwd     = $ScriptDir
        }
        if ($config.mcpServers | Get-Member -Name $s.name -MemberType NoteProperty) {
            $config.mcpServers.($s.name) = $entry
        } else {
            $config.mcpServers | Add-Member -Name $s.name -MemberType NoteProperty -Value $entry
        }
        Write-Host "    Registered $($s.name)"
    }

    $dir = Split-Path $ConfigPath -Parent
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $config | ConvertTo-Json -Depth 10 | Set-Content -Path $ConfigPath -Encoding UTF8
    Write-Ok "Config written: $ConfigPath"
}

# Merge WaterFree MCP servers into a YAML config file (Codex format).
function Merge-YamlMcpConfig([string]$ConfigPath) {
    # Read existing content or start empty
    $lines = if (Test-Path $ConfigPath) {
        (Get-Content $ConfigPath) | Where-Object { $_ -notmatch "^  - name: waterfree-" }
    } else {
        @()
    }

    # Ensure mcp_servers section header exists
    if (-not ($lines -match "^mcp_servers:")) {
        $lines += "mcp_servers:"
    }

    # Append each server block
    foreach ($s in $McpServers) {
        $lines += "  - name: $($s.name)"
        $lines += "    command: $PythonPath"
        $lines += "    args: [""-m"", ""$($s.module)""]"
        $lines += "    cwd: $($ScriptDir -replace '\\','/')"
        Write-Host "    Registered $($s.name)"
    }

    $dir = Split-Path $ConfigPath -Parent
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $lines | Set-Content -Path $ConfigPath -Encoding UTF8
    Write-Ok "Config written: $ConfigPath"
}

# ─── Deploy targets ───────────────────────────────────────────────────────────

function Deploy-Local {
    Write-Step "Building extension..."
    Push-Location $ScriptDir
    try {
        if ($Quick) {
            npm run compile
        } else {
            npm run vscode:prepublish
        }
    } finally {
        Pop-Location
    }

    Write-Step "Packaging VSIX..."
    Push-Location $ScriptDir
    try {
        npx --yes @vscode/vsce package --no-dependencies --out $VsixPath
    } finally {
        Pop-Location
    }

    Write-Step "Installing extension..."
    code --install-extension $VsixPath --force

    Write-Ok "Installed. Use 'Developer: Reload Window' in VS Code to activate."
}

function Deploy-Claude {
    Write-Step "Installing skills for Claude Code..."
    & (Join-Path $SkillsDir "install_claude.ps1")

    Write-Step "Registering MCP servers in Claude Code (~/.claude/claude.json)..."
    Merge-JsonMcpConfig (Join-Path $HOME ".claude\claude.json")

    Write-Ok "Restart Claude Code to activate MCP servers."
}

function Deploy-Codex {
    Write-Step "Installing skills for Codex..."
    & (Join-Path $SkillsDir "install_codex.ps1")

    Write-Step "Registering MCP servers in Codex (~/.codex/config.yaml)..."
    Merge-YamlMcpConfig (Join-Path $HOME ".codex\config.yaml")

    Write-Ok "Restart Codex to activate MCP servers."
}

# ─── Main ─────────────────────────────────────────────────────────────────────

switch ($Target) {
    "local"  { Deploy-Local }
    "claude" { Deploy-Claude }
    "codex"  { Deploy-Codex }
    "all"    { Deploy-Local; Deploy-Claude; Deploy-Codex }
}

Write-Host "`nDone." -ForegroundColor Green
