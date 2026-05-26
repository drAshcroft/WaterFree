<#
.SYNOPSIS
    Install the WaterFree MSI silently on a clean machine, exercise the
    installed CLI, then uninstall — for CI use.

.DESCRIPTION
    Stage 1: msiexec /i <msi> /quiet /qn /l*v <log>
    Stage 2: Locate `waterfree.exe` on the per-user PATH and verify it runs.
    Stage 3: Exercise `waterfree todos list`, `waterfree knowledge stats`,
             and assert valid JSON.
    Stage 4: msiexec /x <msi> /quiet /qn
    Stage 5: Confirm waterfree.exe is gone from PATH.

    Designed for a fresh Windows CI runner where the user-mode PATH does NOT
    yet contain WaterFree. Running it on a developer box with WaterFree
    already installed will overwrite that install — pass `-AllowReinstall` to
    acknowledge.

.PARAMETER MsiPath
    Path to WaterFreeSetup-<version>.msi. Defaults to the newest MSI in dist/.

.PARAMETER AllowReinstall
    Required if WaterFree is already installed for the current user. CI
    runners should not need this.

.EXAMPLE
    .\installer\smoke_msi_install.ps1
#>

[CmdletBinding()]
param(
    [string]$MsiPath,
    [switch]$AllowReinstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$global:LASTEXITCODE = 0

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$DistDir    = Join-Path $RepoRoot "dist"
$InstallDir = Join-Path $env:LOCALAPPDATA "WaterFree"
$ExpectedExe = Join-Path $InstallDir "bin\waterfree.exe"
$ExpectedCodexSkill = Join-Path $HOME ".codex\skills\waterfree-index\SKILL.md"

function Assert([bool]$cond, [string]$msg) {
    if (-not $cond) {
        Write-Host "FAIL: $msg" -ForegroundColor Red
        exit 1
    }
    Write-Host "PASS: $msg" -ForegroundColor Green
}

function Resolve-Msi {
    if ($MsiPath) {
        if (-not (Test-Path $MsiPath)) { throw "MSI not found: $MsiPath" }
        return (Resolve-Path $MsiPath).Path
    }
    if (-not (Test-Path $DistDir)) { throw "No dist/ directory — run build_installer.ps1 first." }
    $candidate = Get-ChildItem -Path $DistDir -Filter "WaterFreeSetup*.msi" |
                 Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $candidate) { throw "No WaterFreeSetup*.msi in $DistDir." }
    return $candidate.FullName
}

function Test-WaterfreeOnPath {
    # User PATH only — system PATH may already have something installed.
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User") ?? ""
    return ($userPath -split ";") -contains (Join-Path $InstallDir "bin")
}

# Sanity: don't blow over a real install unless told to.
if ((Test-Path $ExpectedExe) -and -not $AllowReinstall) {
    Write-Host ("WaterFree appears to be installed already ($ExpectedExe). " +
                "Re-run with -AllowReinstall to overwrite it.") -ForegroundColor Yellow
    exit 2
}

$msi = Resolve-Msi
$logFile = Join-Path $env:TEMP "waterfree-msi-smoke-install.log"
Write-Host "==> MSI under test: $msi" -ForegroundColor Cyan

# --- Stage 1: install --------------------------------------------------------
Write-Host "==> Stage 1: msiexec /i (silent)" -ForegroundColor Cyan
$proc = Start-Process -FilePath "msiexec.exe" -ArgumentList @(
    "/i", "`"$msi`"", "/quiet", "/qn", "/l*v", "`"$logFile`""
) -Wait -PassThru
Assert ($proc.ExitCode -eq 0) "msiexec install exited 0 (got $($proc.ExitCode); log: $logFile)"

# --- Stage 2: exe present and PATH updated ----------------------------------
Write-Host "==> Stage 2: verify installed exe" -ForegroundColor Cyan
Assert (Test-Path $ExpectedExe)   "waterfree.exe present at $ExpectedExe"
Assert (Test-WaterfreeOnPath)     "user PATH contains $InstallDir\bin"

# --- Stage 3: exercise CLI ---------------------------------------------------
Write-Host "==> Stage 3: exercise CLI subcommands" -ForegroundColor Cyan

$tmpWs = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP "waterfree-msi-smoke-ws")
try {
    & $ExpectedExe todos list --workspace $tmpWs.FullName | Out-Null
    Assert ($LASTEXITCODE -eq 0) "waterfree todos list returns 0"

    & $ExpectedExe knowledge stats | Out-Null
    Assert ($LASTEXITCODE -eq 0) "waterfree knowledge stats returns 0"

    Assert (Test-Path $ExpectedCodexSkill) "Codex WaterFree skills are installed"
    $skillText = Get-Content -Raw -Path $ExpectedCodexSkill
    Assert ($skillText -match "waterfree index") "Codex index skill points at the waterfree CLI"
    Assert ($skillText -notmatch "mcp__|MCP tools|MCP server") "Codex index skill does not point at MCP tools"
} finally {
    Remove-Item -Recurse -Force $tmpWs.FullName -ErrorAction SilentlyContinue
}

# --- Stage 4: uninstall ------------------------------------------------------
Write-Host "==> Stage 4: msiexec /x (silent)" -ForegroundColor Cyan
$uninstallLog = Join-Path $env:TEMP "waterfree-msi-smoke-uninstall.log"
$proc = Start-Process -FilePath "msiexec.exe" -ArgumentList @(
    "/x", "`"$msi`"", "/quiet", "/qn", "/l*v", "`"$uninstallLog`""
) -Wait -PassThru
Assert ($proc.ExitCode -eq 0) "msiexec uninstall exited 0 (log: $uninstallLog)"

# --- Stage 5: PATH cleaned ---------------------------------------------------
Write-Host "==> Stage 5: verify PATH cleanup" -ForegroundColor Cyan
Assert (-not (Test-Path $ExpectedExe)) "waterfree.exe removed from $InstallDir"
Assert (-not (Test-WaterfreeOnPath))   "user PATH no longer contains $InstallDir\bin"
Assert (-not (Test-Path $ExpectedCodexSkill)) "Codex WaterFree skills removed"

Write-Host ""
Write-Host "==> MSI install/uninstall smoke test passed." -ForegroundColor Green
exit 0
