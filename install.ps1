[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $HOME ".waterfree"),
    [string]$PythonPath = "",
    [switch]$SkipCodex,
    [switch]$SkipClaude,
    [switch]$SkipVenvRefresh
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$RepoRoot = $PSScriptRoot
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupRoot = Join-Path $InstallRoot "install\backups\$Timestamp"
$BinDir = Join-Path $InstallRoot "bin"
$RuntimeRoot = Join-Path $InstallRoot "runtime\current"
$RuntimeBackendDir = Join-Path $RuntimeRoot "backend"
$RuntimeVenvDir = Join-Path $InstallRoot "runtime\venv"
$RuntimePython = Join-Path $RuntimeVenvDir "Scripts\python.exe"
$ManifestDir = Join-Path $InstallRoot "install"
$ManifestPath = Join-Path $ManifestDir "manifest.json"
$LogDir = Join-Path $InstallRoot "logs\mcp"
$LauncherPs1 = Join-Path $BinDir "waterfree-mcp.ps1"
$LauncherCmd = Join-Path $BinDir "waterfree-mcp.cmd"
$KnowledgeDbPath = Join-Path $InstallRoot "global\knowledge.db"
$ClaudeConfigPath = Join-Path $HOME ".claude.json"
$CodexConfigPath = Join-Path $HOME ".codex\config.toml"

$Servers = @(
    @{ name = "waterfree-index"; mode = "index"; module = "backend.mcp_index" }
    @{ name = "waterfree-knowledge"; mode = "knowledge"; module = "backend.mcp_knowledge" }
    @{ name = "waterfree-todos"; mode = "todos"; module = "backend.mcp_todos" }
    @{ name = "waterfree-debug"; mode = "debug"; module = "backend.mcp_debug" }
    @{ name = "waterfree-testing"; mode = "testing"; module = "backend.mcp_testing" }
)

$LegacyServerNames = @(
    "pairprogram-debug",
    "pairprogram-index",
    "pairprogram-knowledge",
    "pairprogram-todos"
)

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "    $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "    $Message" -ForegroundColor Yellow
}

function Assert-LastExitCode([string]$CommandName) {
    if ($LASTEXITCODE -ne 0) {
        throw "$CommandName failed with exit code $LASTEXITCODE."
    }
}

function Ensure-Directory([string]$Path) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

function Get-NormalizedFullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Assert-PathUnderRoot([string]$Path, [string]$Root) {
    $fullPath = Get-NormalizedFullPath $Path
    $fullRoot = Get-NormalizedFullPath $Root
    if (-not $fullPath.StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify '$fullPath' because it is outside '$fullRoot'."
    }
}

function Backup-File([string]$Path) {
    if (-not (Test-Path $Path)) {
        return $null
    }

    Ensure-Directory $BackupRoot
    $dest = Join-Path $BackupRoot ([System.IO.Path]::GetFileName($Path))
    Copy-Item -LiteralPath $Path -Destination $dest -Force
    return $dest
}

function Resolve-BootstrapPython {
    $candidates = @()
    if ($PythonPath) {
        $candidates += $PythonPath
    }

    $repoVenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $repoVenvPython) {
        $candidates += $repoVenvPython
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source -notmatch "WindowsApps\\python.exe$") {
        $candidates += $pythonCmd.Source
    }

    foreach ($candidate in $candidates) {
        if (-not $candidate) {
            continue
        }

        $resolved = $candidate
        if (Test-Path $candidate) {
            $resolved = (Resolve-Path $candidate).Path
        }

        try {
            & $resolved -c "import sys; print(sys.executable)" | Out-Null
            Assert-LastExitCode "python probe"
            return $resolved
        } catch {
            continue
        }
    }

    throw "Could not find a working Python interpreter. Pass -PythonPath explicitly."
}

function Write-LauncherFiles {
    $ps1 = @'
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("index", "knowledge", "todos", "debug", "testing")]
    [string]$Mode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$installRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $installRoot "install\manifest.json"
if (-not (Test-Path $manifestPath)) {
    throw "WaterFree manifest not found at $manifestPath"
}

$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
$python = $manifest.runtime.python
$runtimeRoot = $manifest.runtime.root

$moduleMap = @{
    index = "backend.mcp_index"
    knowledge = "backend.mcp_knowledge"
    todos = "backend.mcp_todos"
    debug = "backend.mcp_debug"
    testing = "backend.mcp_testing"
}

$env:PYTHONPATH = $runtimeRoot
$env:WATERFREE_MCP_LOG_DIR = Join-Path $installRoot "logs\mcp"
$env:PYTHONUNBUFFERED = "1"

& $python -m $moduleMap[$Mode]
exit $LASTEXITCODE
'@

    $cmd = @'
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0waterfree-mcp.ps1" %*
exit /b %errorlevel%
'@

    Ensure-Directory $BinDir
    Set-Content -Path $LauncherPs1 -Value $ps1 -Encoding ASCII
    Set-Content -Path $LauncherCmd -Value $cmd -Encoding ASCII
}

function ConvertTo-McpServerMap($Value) {
    $map = [ordered]@{}
    if ($null -eq $Value) {
        return $map
    }

    if ($Value -is [System.Collections.IEnumerable] -and
        $Value -isnot [string] -and
        $Value.PSObject.Properties.Name -notcontains "Name") {
        foreach ($item in $Value) {
            $name = $item.name
            if (-not $name) {
                continue
            }

            $entry = [ordered]@{}
            foreach ($prop in $item.PSObject.Properties) {
                if ($prop.Name -eq "name") {
                    continue
                }
                $entry[$prop.Name] = $prop.Value
            }
            $map[$name] = [PSCustomObject]$entry
        }
        return $map
    }

    foreach ($prop in $Value.PSObject.Properties) {
        $map[$prop.Name] = $prop.Value
    }
    return $map
}

function ConvertFrom-McpServerMap($Map, [bool]$AsArray) {
    if ($AsArray) {
        $items = New-Object System.Collections.Generic.List[object]
        foreach ($name in $Map.Keys) {
            $entry = [ordered]@{ name = $name }
            $value = $Map[$name]
            foreach ($prop in $value.PSObject.Properties) {
                $entry[$prop.Name] = $prop.Value
            }
            $items.Add([PSCustomObject]$entry)
        }
        return @($items.ToArray())
    }

    $obj = [PSCustomObject]@{}
    foreach ($name in $Map.Keys) {
        $obj | Add-Member -MemberType NoteProperty -Name $name -Value $Map[$name]
    }
    return $obj
}

function New-ClaudeMcpEntry([string]$Mode) {
    return [ordered]@{
        command = $LauncherCmd
        args = @($Mode)
    }
}

function Merge-ClaudeConfig {
    $config = if (Test-Path $ClaudeConfigPath) {
        Get-Content $ClaudeConfigPath -Raw | ConvertFrom-Json
    } else {
        [PSCustomObject]@{}
    }

    $existingMcpServers = if ($config.PSObject.Properties.Name -contains "mcpServers") {
        $config.mcpServers
    } else {
        $null
    }

    $mcpServersAsArray = $existingMcpServers -is [System.Collections.IEnumerable] -and
        $existingMcpServers -isnot [string] -and
        $existingMcpServers.PSObject.Properties.Name -notcontains "Name"
    $mcpServerMap = ConvertTo-McpServerMap -Value $existingMcpServers

    foreach ($legacyName in $LegacyServerNames) {
        if ($mcpServerMap.Contains($legacyName)) {
            $mcpServerMap.Remove($legacyName)
        }
    }

    foreach ($server in $Servers) {
        $mcpServerMap[$server.name] = [PSCustomObject](New-ClaudeMcpEntry -Mode $server.mode)
    }

    $normalizedMcpServers = ConvertFrom-McpServerMap -Map $mcpServerMap -AsArray:$mcpServersAsArray
    if ($config.PSObject.Properties.Name -contains "mcpServers") {
        $config.mcpServers = $normalizedMcpServers
    } else {
        $config | Add-Member -MemberType NoteProperty -Name mcpServers -Value $normalizedMcpServers
    }

    $dir = Split-Path $ClaudeConfigPath -Parent
    if ($dir) {
        Ensure-Directory $dir
    }
    $config | ConvertTo-Json -Depth 50 | Set-Content -Path $ClaudeConfigPath -Encoding UTF8
}

function Register-Codex {
    $codex = Get-Command codex -ErrorAction SilentlyContinue
    if (-not $codex) {
        Write-Warn "Codex CLI not found; skipping Codex registration."
        return
    }

    Write-Step "Registering MCP servers in Codex..."

    foreach ($legacyName in $LegacyServerNames) {
        & $codex.Source mcp remove $legacyName 2>&1 | Out-Null
    }

    foreach ($server in $Servers) {
        & $codex.Source mcp remove $server.name 2>&1 | Out-Null
        & $codex.Source mcp add $server.name -- $LauncherCmd $server.mode 2>&1 | Out-Null
        Assert-LastExitCode "codex mcp add $($server.name)"
        & $codex.Source mcp get $server.name --json 2>&1 | Out-Null
        Assert-LastExitCode "codex mcp get $($server.name)"
        Write-Ok "Codex registered $($server.name)"
    }
}

function Register-Claude {
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    if ($claude) {
        Write-Step "Registering MCP servers in Claude Code via CLI..."
        foreach ($legacyName in $LegacyServerNames) {
            & $claude.Source mcp remove $legacyName 2>&1 | Out-Null
        }

        foreach ($server in $Servers) {
            & $claude.Source mcp remove $server.name 2>&1 | Out-Null
            & $claude.Source mcp add --scope user $server.name -- $LauncherCmd $server.mode 2>&1 | Out-Null
            Assert-LastExitCode "claude mcp add $($server.name)"
            Write-Ok "Claude registered $($server.name)"
        }
        return
    }

    Write-Step "Claude CLI not found; writing user-scoped MCP config to $ClaudeConfigPath..."
    Merge-ClaudeConfig
    Write-Ok "Claude user config updated."
}

function Smoke-TestLauncher([string]$Mode) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell"
    $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$LauncherPs1`" $Mode"
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    $null = $process.Start()
    Start-Sleep -Seconds 2

    if ($process.HasExited) {
        $stderr = $process.StandardError.ReadToEnd()
        $stdout = $process.StandardOutput.ReadToEnd()
        throw "Launcher mode '$Mode' exited early. stdout=$stdout stderr=$stderr"
    }

    try {
        $process.Kill($true)
        $process.WaitForExit()
    } catch {
        # Best effort cleanup only.
    }
}

Write-Step "Preparing WaterFree install directories..."
Ensure-Directory $InstallRoot
Ensure-Directory $ManifestDir
Ensure-Directory $LogDir

Write-Step "Backing up important files..."
$knowledgeBackup = Backup-File $KnowledgeDbPath
$claudeBackup = Backup-File $ClaudeConfigPath
$codexBackup = Backup-File $CodexConfigPath
if ($knowledgeBackup) { Write-Ok "Knowledge DB backup: $knowledgeBackup" } else { Write-Ok "Knowledge DB not modified; no backup source found." }
if ($claudeBackup) { Write-Ok "Claude config backup: $claudeBackup" }
if ($codexBackup) { Write-Ok "Codex config backup: $codexBackup" }

$bootstrapPython = Resolve-BootstrapPython
Write-Ok "Bootstrap Python: $bootstrapPython"

Write-Step "Installing backend runtime under $RuntimeRoot..."
Ensure-Directory $RuntimeRoot
if (Test-Path $RuntimeBackendDir) {
    Assert-PathUnderRoot -Path $RuntimeBackendDir -Root $InstallRoot
    Remove-Item -LiteralPath $RuntimeBackendDir -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $RepoRoot "backend") -Destination $RuntimeRoot -Recurse -Force
Write-Ok "Copied backend runtime."

if (-not (Test-Path $RuntimePython)) {
    Write-Step "Creating private Python runtime..."
    & $bootstrapPython -m venv $RuntimeVenvDir
    Assert-LastExitCode "python -m venv"
}

if (-not $SkipVenvRefresh) {
    Write-Step "Installing Python dependencies into private runtime..."
    & $RuntimePython -m pip install --disable-pip-version-check -r (Join-Path $RuntimeBackendDir "requirements.txt")
    Assert-LastExitCode "pip install -r requirements.txt"
    Write-Ok "Python dependencies installed."
}

Write-Step "Writing shared WaterFree launcher..."
Write-LauncherFiles
Write-Ok "Launcher created: $LauncherCmd"

$manifest = [ordered]@{
    installedAt = (Get-Date).ToString("o")
    sourceRepo = $RepoRoot
    knowledgeDbPath = $KnowledgeDbPath
    launcher = [ordered]@{
        cmd = $LauncherCmd
        ps1 = $LauncherPs1
    }
    runtime = [ordered]@{
        root = $RuntimeRoot
        python = $RuntimePython
    }
    logDir = $LogDir
    servers = @(
        foreach ($server in $Servers) {
            [ordered]@{
                name = $server.name
                mode = $server.mode
                module = $server.module
            }
        }
    )
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -Path $ManifestPath -Encoding UTF8
Write-Ok "Manifest written: $ManifestPath"

Write-Step "Smoke-testing the installed launcher..."
foreach ($mode in @("knowledge", "index", "todos", "debug", "testing")) {
    Smoke-TestLauncher -Mode $mode
    Write-Ok "Launcher mode '$mode' stayed alive under smoke test."
}

if (-not $SkipCodex) {
    Register-Codex
}

if (-not $SkipClaude) {
    Register-Claude
}

Write-Host "`nInstall complete." -ForegroundColor Green
