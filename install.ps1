<#
.SYNOPSIS
    Install WaterFree MCP servers and register them with AI coding tools.

.DESCRIPTION
    Copies the pre-built WaterFree executable to ~/.waterfree/bin/ and
    registers the MCP servers with Claude Code, Codex, and Kilo Code.

    No Python installation required.

.PARAMETER InstallRoot
    Where to install WaterFree runtime files. Defaults to ~/.waterfree.

.PARAMETER ExePath
    Path to the waterfree executable. Defaults to auto-detection from
    the repo bin/ directory or the installed VS Code extension.

.PARAMETER SkipCodex
    Skip Codex MCP registration.

.PARAMETER SkipClaude
    Skip Claude Code MCP registration.
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $HOME ".waterfree"),
    [string]$ExePath = "",
    [switch]$SkipCodex,
    [switch]$SkipClaude
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$RepoRoot       = $PSScriptRoot
$Timestamp      = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupRoot     = Join-Path $InstallRoot "install\backups\$Timestamp"
$BinDir         = Join-Path $InstallRoot "bin"
$ManifestDir    = Join-Path $InstallRoot "install"
$ManifestPath   = Join-Path $ManifestDir "manifest.json"
$LogDir         = Join-Path $InstallRoot "logs\mcp"
$VsixPath       = Join-Path $RepoRoot "waterfree.vsix"
$ClaudeConfigPath   = Join-Path $HOME ".claude.json"
$CodexConfigPath    = Join-Path $HOME ".codex\config.toml"

$CodeCmd = @(
    "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd",
    "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd",
    (Get-Command code.cmd -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $CodeCmd) { $CodeCmd = "code" }


$Servers = @(
    @{ name = "waterfree-index";      mode = "index" }
    @{ name = "waterfree-knowledge";  mode = "knowledge" }
    @{ name = "waterfree-todos";      mode = "todos" }
    @{ name = "waterfree-debug";      mode = "debug" }
    @{ name = "waterfree-testing";    mode = "testing" }
    @{ name = "waterfree-qa-summary"; mode = "qa-summary" }
)

$LegacyServerNames = @(
    "pairprogram-debug",
    "pairprogram-index",
    "pairprogram-knowledge",
    "pairprogram-todos"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "    $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "    $Message" -ForegroundColor Yellow }

function Assert-LastExitCode([string]$CommandName) {
    if ($LASTEXITCODE -ne 0) { throw "$CommandName failed with exit code $LASTEXITCODE." }
}

function Invoke-BestEffortNative([scriptblock]$Command, [string]$Description) {
    $hasPref = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    $prev = $null
    if ($hasPref) { $prev = $PSNativeCommandUseErrorActionPreference; $PSNativeCommandUseErrorActionPreference = $false }
    try { & $Command 2>&1 | Out-Null } catch { Write-Warn "$Description (continuing): $($_.Exception.Message)" }
    finally { if ($hasPref) { $PSNativeCommandUseErrorActionPreference = $prev } }
}

function Ensure-Directory([string]$Path) { New-Item -ItemType Directory -Force -Path $Path | Out-Null }

function Get-NormalizedFullPath([string]$Path) { [System.IO.Path]::GetFullPath($Path) }

function Assert-PathUnderRoot([string]$Path, [string]$Root) {
    $fp = Get-NormalizedFullPath $Path
    $fr = Get-NormalizedFullPath $Root
    if (-not $fp.StartsWith($fr, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify '$fp': outside '$fr'."
    }
}

function Backup-File([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    Ensure-Directory $BackupRoot
    $dest = Join-Path $BackupRoot ([System.IO.Path]::GetFileName($Path))
    Copy-Item -LiteralPath $Path -Destination $dest -Force
    return $dest
}

# ---------------------------------------------------------------------------
# Find the pre-built executable
# ---------------------------------------------------------------------------

function Resolve-WaterfreeExe {
    $arch    = if ([System.Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
    $exeName = "waterfree-win32-$arch.exe"

    $candidates = @()
    if ($ExePath) { $candidates += $ExePath }

    # Repo bin/ (dev/build scenario)
    $candidates += Join-Path $RepoRoot "bin\$exeName"

    # Installed VS Code extension
    $extRoot = Join-Path $HOME ".vscode\extensions"
    if (Test-Path $extRoot) {
        Get-ChildItem $extRoot -Directory |
            Where-Object { $_.Name -like "waterfree.waterfree*" } |
            Sort-Object LastWriteTime -Descending |
            ForEach-Object { $candidates += Join-Path $_.FullName "bin\$exeName" }
    }

    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path }
    }

    throw @"
WaterFree executable not found ($exeName).
Run build.ps1 first, or pass -ExePath explicitly.
"@
}

# ---------------------------------------------------------------------------
# Config helpers (JSON MCP registration)
# ---------------------------------------------------------------------------

function ConvertTo-McpServerMap($Value) {
    $map = [ordered]@{}
    if ($null -eq $Value) { return $map }

    if ($Value -is [System.Collections.IEnumerable] -and
        $Value -isnot [string] -and
        $Value.PSObject.Properties.Name -notcontains "Name") {
        foreach ($item in $Value) {
            $name = $item.name
            if (-not $name) { continue }
            $entry = [ordered]@{}
            foreach ($prop in $item.PSObject.Properties) {
                if ($prop.Name -eq "name") { continue }
                $entry[$prop.Name] = $prop.Value
            }
            $map[$name] = [PSCustomObject]$entry
        }
        return $map
    }

    foreach ($prop in $Value.PSObject.Properties) { $map[$prop.Name] = $prop.Value }
    return $map
}

function ConvertFrom-McpServerMap($Map, [bool]$AsArray) {
    if ($AsArray) {
        $items = New-Object System.Collections.Generic.List[object]
        foreach ($name in $Map.Keys) {
            $entry = [ordered]@{ name = $name }
            foreach ($prop in $Map[$name].PSObject.Properties) { $entry[$prop.Name] = $prop.Value }
            $items.Add([PSCustomObject]$entry)
        }
        return @($items.ToArray())
    }
    $obj = [PSCustomObject]@{}
    foreach ($name in $Map.Keys) { $obj | Add-Member -MemberType NoteProperty -Name $name -Value $Map[$name] }
    return $obj
}

function New-McpEntry([string]$InstallExe, [string]$Mode) {
    return [ordered]@{
        command = $InstallExe
        args    = @("mcp", $Mode)
    }
}

function Merge-ClaudeConfig([string]$InstallExe) {
    $config = if (Test-Path $ClaudeConfigPath) {
        Get-Content $ClaudeConfigPath -Raw | ConvertFrom-Json
    } else { [PSCustomObject]@{} }

    $existing = if ($config.PSObject.Properties.Name -contains "mcpServers") { $config.mcpServers } else { $null }
    $asArray  = $existing -is [System.Collections.IEnumerable] -and $existing -isnot [string] -and $existing.PSObject.Properties.Name -notcontains "Name"
    $map      = ConvertTo-McpServerMap -Value $existing

    foreach ($n in $LegacyServerNames) { if ($map.Contains($n)) { $map.Remove($n) } }
    foreach ($s in $Servers) { $map[$s.name] = [PSCustomObject](New-McpEntry -InstallExe $InstallExe -Mode $s.mode) }

    $normalized = ConvertFrom-McpServerMap -Map $map -AsArray:$asArray
    if ($config.PSObject.Properties.Name -contains "mcpServers") {
        $config.mcpServers = $normalized
    } else {
        $config | Add-Member -MemberType NoteProperty -Name mcpServers -Value $normalized
    }

    Ensure-Directory (Split-Path $ClaudeConfigPath -Parent)
    $config | ConvertTo-Json -Depth 50 | Set-Content -Path $ClaudeConfigPath -Encoding UTF8
}

# ---------------------------------------------------------------------------
# Registration functions
# ---------------------------------------------------------------------------

function Register-Codex([string]$InstallExe) {
    $codex = Get-Command codex -ErrorAction SilentlyContinue
    if (-not $codex) { Write-Warn "Codex CLI not found; skipping."; return }

    Write-Step "Registering MCP servers in Codex..."
    foreach ($n in $LegacyServerNames) {
        Invoke-BestEffortNative -Description "codex remove $n" -Command { & $codex.Source mcp remove $n }
    }
    foreach ($s in $Servers) {
        Invoke-BestEffortNative -Description "codex remove $($s.name)" -Command { & $codex.Source mcp remove $s.name }
        & $codex.Source mcp add $s.name -- $InstallExe mcp $s.mode 2>&1 | Out-Null
        Assert-LastExitCode "codex mcp add $($s.name)"
        Write-Ok "Codex: $($s.name)"
    }
}

function Register-Claude([string]$InstallExe) {
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    if ($claude) {
        Write-Step "Registering MCP servers in Claude Code via CLI..."
        foreach ($n in $LegacyServerNames) {
            Invoke-BestEffortNative -Description "claude remove $n" -Command { & $claude.Source mcp remove $n }
        }
        foreach ($s in $Servers) {
            Invoke-BestEffortNative -Description "claude remove $($s.name)" -Command { & $claude.Source mcp remove $s.name }
            & $claude.Source mcp add --scope user $s.name -- $InstallExe mcp $s.mode 2>&1 | Out-Null
            Assert-LastExitCode "claude mcp add $($s.name)"
            Write-Ok "Claude: $($s.name)"
        }
        return
    }

    Write-Step "Claude CLI not found; writing config to $ClaudeConfigPath..."
    Merge-ClaudeConfig -InstallExe $InstallExe
    Write-Ok "Claude config updated."
}

function Invoke-SmokeTest([string]$InstallExe, [string]$Mode) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName              = $InstallExe
    $psi.Arguments             = "mcp $Mode"
    $psi.UseShellExecute       = $false
    $psi.CreateNoWindow        = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    $null = $proc.Start()
    Start-Sleep -Seconds 2

    if ($proc.HasExited) {
        $stderr = $proc.StandardError.ReadToEnd()
        $stdout = $proc.StandardOutput.ReadToEnd()
        throw "MCP mode '$Mode' exited immediately. stdout=$stdout stderr=$stderr"
    }

    try { $proc.Kill($true); $proc.WaitForExit() } catch { }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-Step "Preparing install directories..."
Ensure-Directory $InstallRoot
Ensure-Directory $ManifestDir
Ensure-Directory $LogDir
Ensure-Directory $BinDir

Write-Step "Backing up config files..."
$claudeBackup = Backup-File $ClaudeConfigPath
$codexBackup  = Backup-File $CodexConfigPath
if ($claudeBackup) { Write-Ok "Claude backup: $claudeBackup" }
if ($codexBackup)  { Write-Ok "Codex backup:  $codexBackup" }

if (Test-Path $VsixPath) {
    Write-Step "Installing VS Code extension..."
    $installOut = & $CodeCmd --install-extension $VsixPath --force 2>&1
    if ($LASTEXITCODE -ne 0 -and ($installOut -match "EBUSY|restart VS Code")) {
        Write-Warn "Extension is locked. Close VS Code or run 'Developer: Reload Window', then re-run."
    } elseif ($LASTEXITCODE -ne 0) {
        throw "VS Code extension install failed (exit $LASTEXITCODE): $installOut"
    } else {
        Write-Ok "VS Code extension installed."
    }
} else {
    Write-Warn "No waterfree.vsix found in repo root — skipping VS Code extension install."
    Write-Warn "Run deploy.ps1 local first to build the VSIX."
}

Write-Step "Locating WaterFree executable..."
$sourceExe  = Resolve-WaterfreeExe
$installExe = Join-Path $BinDir ([System.IO.Path]::GetFileName($sourceExe))

if ((Get-NormalizedFullPath $sourceExe) -ne (Get-NormalizedFullPath $installExe)) {
    # Stop any running waterfree processes that would lock the file
    Get-Process | Where-Object { $_.Path -eq $installExe } | ForEach-Object {
        Write-Warn "Stopping running WaterFree process (pid=$($_.Id))..."
        $_ | Stop-Process -Force
    }
    Copy-Item -LiteralPath $sourceExe -Destination $installExe -Force
    Write-Ok "Installed: $installExe"
} else {
    Write-Ok "Exe already in place: $installExe"
}

Write-Step "Smoke-testing MCP servers..."
foreach ($s in $Servers) {
    Invoke-SmokeTest -InstallExe $installExe -Mode $s.mode
    Write-Ok "  $($s.mode) — OK"
}

$manifest = [ordered]@{
    installedAt = (Get-Date).ToString("o")
    sourceExe   = $sourceExe
    exe         = $installExe
    logDir      = $LogDir
    servers     = @(foreach ($s in $Servers) { [ordered]@{ name = $s.name; mode = $s.mode } })
}
$manifest | ConvertTo-Json -Depth 10 | Set-Content -Path $ManifestPath -Encoding UTF8
Write-Ok "Manifest: $ManifestPath"

if (-not $SkipCodex) { Register-Codex  -InstallExe $installExe }
if (-not $SkipClaude) { Register-Claude -InstallExe $installExe }

Write-Host "`nInstall complete." -ForegroundColor Green
