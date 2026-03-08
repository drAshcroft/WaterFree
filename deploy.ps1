<#
.SYNOPSIS
    Deploy the WaterFree VS Code extension and/or MCP integrations.

.DESCRIPTION
    Targets:
      local       - Build VSIX and install to VS Code
      claude      - Install skills + register MCP servers in Claude Code
      codex       - Install skills + register MCP servers in Codex
      kilo        - Register MCP servers in Kilo Code
      interactive - Launch the guided installer
      all         - local + claude + codex + kilo
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("local", "claude", "codex", "kilo", "interactive", "all")]
    [string]$Target = "local",

    [switch]$Quick,

    [string]$PythonPath = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
$SkillsDir = Join-Path $ScriptDir "skills"
$VsixPath = Join-Path $ScriptDir "waterfree.vsix"
$McpLauncherPath = Join-Path $ScriptDir "scripts\start_mcp_server.ps1"

$CodeCmd = @(
    "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd",
    "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd",
    (Get-Command code.cmd -ErrorAction SilentlyContinue)?.Source
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $CodeCmd) {
    $CodeCmd = "code"
}

$McpServers = @(
    @{ name = "waterfree-debug"; module = "backend.mcp_debug" }
    @{ name = "waterfree-index"; module = "backend.mcp_index" }
    @{ name = "waterfree-knowledge"; module = "backend.mcp_knowledge" }
    @{ name = "waterfree-todos"; module = "backend.mcp_todos" }
    @{ name = "waterfree-testing"; module = "backend.mcp_testing" }
)

$LegacyMcpServerNames = @(
    "waterfree-debug",
    "waterfree-index",
    "waterfree-knowledge",
    "waterfree-todos"
)

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "    $Message" -ForegroundColor Green
}

function New-McpEntry([string]$Module) {
    return [ordered]@{
        command = "powershell"
        args = @(
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            $McpLauncherPath,
            "-Module",
            $Module,
            "-PythonPath",
            $PythonPath,
            "-WorkingDirectory",
            $ScriptDir
        )
        cwd = $ScriptDir
    }
}

function Merge-JsonMcpConfig([string]$ConfigPath) {
    $config = if (Test-Path $ConfigPath) {
        Get-Content $ConfigPath -Raw | ConvertFrom-Json
    } else {
        [PSCustomObject]@{}
    }

    if (-not ($config.PSObject.Properties.Name -contains "mcpServers")) {
        $config | Add-Member -MemberType NoteProperty -Name mcpServers -Value ([PSCustomObject]@{})
    }

    foreach ($legacyName in $LegacyMcpServerNames) {
        if ($config.mcpServers.PSObject.Properties.Name -contains $legacyName) {
            $config.mcpServers.PSObject.Properties.Remove($legacyName)
            Write-Host "    Removed legacy $legacyName"
        }
    }

    foreach ($server in $McpServers) {
        $entry = New-McpEntry -Module $server.module
        if ($config.mcpServers.PSObject.Properties.Name -contains $server.name) {
            $config.mcpServers.($server.name) = $entry
        } else {
            $config.mcpServers | Add-Member -MemberType NoteProperty -Name $server.name -Value $entry
        }
        Write-Host "    Registered $($server.name)"
    }

    $dir = Split-Path $ConfigPath -Parent
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $config | ConvertTo-Json -Depth 10 | Set-Content -Path $ConfigPath -Encoding UTF8
    Write-Ok "Config written: $ConfigPath"
}

function Remove-WaterFreeYamlBlocks([string[]]$Lines) {
    $result = New-Object System.Collections.Generic.List[string]
    $skipping = $false
    foreach ($line in $Lines) {
        if ($line -match '^\s*-\s+name:\s+(waterfree-|waterfree-)') {
            $skipping = $true
            continue
        }

        if ($skipping) {
            if ($line -match '^\s*-\s+name:\s+' -or $line -match '^[A-Za-z0-9_]+\s*:') {
                $skipping = $false
            } else {
                continue
            }
        }

        $result.Add($line)
    }
    return $result.ToArray()
}

function Merge-YamlMcpConfig([string]$ConfigPath) {
    $lines = if (Test-Path $ConfigPath) { Get-Content $ConfigPath } else { @() }
    $lines = Remove-WaterFreeYamlBlocks -Lines $lines

    $output = New-Object System.Collections.Generic.List[string]
    $inserted = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $output.Add($line)
        if (-not $inserted -and $line -match '^mcp_servers:\s*$') {
            foreach ($server in $McpServers) {
                $entry = New-McpEntry -Module $server.module
                $output.Add("  - name: $($server.name)")
                $output.Add('    command: "powershell"')
                $argsYaml = ($entry.args | ForEach-Object { '"' + ($_ -replace '\\', '/') + '"' }) -join ", "
                $output.Add("    args: [$argsYaml]")
                $output.Add('    cwd: "' + ($entry.cwd -replace '\\', '/') + '"')
                Write-Host "    Registered $($server.name)"
            }
            $inserted = $true
        }
    }

    if (-not $inserted) {
        $output.Add("mcp_servers:")
        foreach ($server in $McpServers) {
            $entry = New-McpEntry -Module $server.module
            $output.Add("  - name: $($server.name)")
            $output.Add('    command: "powershell"')
            $argsYaml = ($entry.args | ForEach-Object { '"' + ($_ -replace '\\', '/') + '"' }) -join ", "
            $output.Add("    args: [$argsYaml]")
            $output.Add('    cwd: "' + ($entry.cwd -replace '\\', '/') + '"')
            Write-Host "    Registered $($server.name)"
        }
    }

    $dir = Split-Path $ConfigPath -Parent
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $output | Set-Content -Path $ConfigPath -Encoding UTF8
    Write-Ok "Config written: $ConfigPath"
}

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
        npx --yes @vscode/vsce package --no-dependencies --out $VsixPath --baseContentUrl https://localhost
    } finally {
        Pop-Location
    }

    Write-Step "Installing extension..."
    $installOut = & $CodeCmd --install-extension $VsixPath --force 2>&1
    if ($LASTEXITCODE -ne 0 -and ($installOut -match "EBUSY|restart VS Code")) {
        Write-Host "    Extension is locked by a running VS Code instance." -ForegroundColor Yellow
        Write-Host "    Close VS Code or run 'Developer: Reload Window', then re-run the installer." -ForegroundColor Yellow
    } else {
        Write-Host $installOut
        Write-Ok "Installed. Use 'Developer: Reload Window' in VS Code to activate."
    }
}

function Deploy-Claude {
    Write-Step "Installing skills for Claude Code..."
    & (Join-Path $SkillsDir "install_claude.ps1")

    Write-Step "Registering MCP servers in Claude Code (~/.claude/claude.json)..."
    Merge-JsonMcpConfig -ConfigPath (Join-Path $HOME ".claude\claude.json")

    Write-Ok "Restart Claude Code to activate MCP servers."
}

function Deploy-Codex {
    Write-Step "Installing skills for Codex..."
    & (Join-Path $SkillsDir "install_codex.ps1")

    Write-Step "Registering MCP servers in Codex (~/.codex/config.yaml)..."
    Merge-YamlMcpConfig -ConfigPath (Join-Path $HOME ".codex\config.yaml")

    Write-Ok "Restart Codex to activate MCP servers."
}

function Deploy-Kilo {
    $kiloConfigPath = Join-Path $env:APPDATA "Kilo Code\User\globalStorage\kilocode.kilo-code\settings\mcp_settings.json"
    Write-Step "Registering MCP servers in Kilo Code ($kiloConfigPath)..."
    Merge-JsonMcpConfig -ConfigPath $kiloConfigPath

    Write-Ok "Restart Kilo Code to activate MCP servers."
}

function Run-InteractiveInstaller {
    & (Join-Path $ScriptDir "install.ps1") -Quick:$Quick -PythonPath $PythonPath
}

switch ($Target) {
    "local" { Deploy-Local }
    "claude" { Deploy-Claude }
    "codex" { Deploy-Codex }
    "kilo" { Deploy-Kilo }
    "interactive" { Run-InteractiveInstaller }
    "all" {
        Deploy-Local
        Deploy-Claude
        Deploy-Codex
        Deploy-Kilo
    }
}

Write-Host "`nDone." -ForegroundColor Green
