<#
.SYNOPSIS
    Build WaterFree MSI installer (WiX v4).

.DESCRIPTION
    Builds the backend executable (if missing), compiles the installer helper,
    then packages everything into an MSI using WiX.

.PARAMETER ProductVersion
    MSI product version (defaults to package.json version).

.PARAMETER Configuration
    Build configuration for the helper (Release by default).

.PARAMETER NoBuildBackend
    Skip backend build even if the executable is missing.
#>

[CmdletBinding()]
param(
    [string]$ProductVersion = "",
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [switch]$NoBuildBackend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PayloadDir = Join-Path $PSScriptRoot "out\payload"
$OutDir = Join-Path $PSScriptRoot "out"
$HelperOut = Join-Path $OutDir "helper"
$WxsPath = Join-Path $PSScriptRoot "WaterFreeInstaller.wxs"

function Write-Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok([string]$m)   { Write-Host "    $m" -ForegroundColor Green }

function Assert-Exit([string]$cmd) {
    if ($LASTEXITCODE -ne 0) { throw "$cmd failed (exit $LASTEXITCODE)" }
}

if (-not $ProductVersion) {
    $pkg = Get-Content (Join-Path $RepoRoot "package.json") -Raw | ConvertFrom-Json
    $ProductVersion = [string]$pkg.version
}

Write-Step "Preparing output directories..."
New-Item -ItemType Directory -Force -Path $PayloadDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-Item -ItemType Directory -Force -Path $HelperOut | Out-Null

Write-Step "Ensuring backend executable..."
$arch = if ([System.Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
$builtExe = Join-Path $RepoRoot "bin\waterfree-win32-$arch.exe"
if (-not (Test-Path $builtExe)) {
    if ($NoBuildBackend) {
        throw "Missing backend exe ($builtExe). Run build.ps1 first or omit -NoBuildBackend."
    }
    & (Join-Path $RepoRoot "build.ps1")
    Assert-Exit "build.ps1"
}

Copy-Item -LiteralPath $builtExe -Destination (Join-Path $PayloadDir "waterfree-mcp.exe") -Force
Write-Ok "Payload: waterfree-mcp.exe"

Write-Step "Building installer helper..."
$helperProject = Join-Path $PSScriptRoot "WaterFreeInstallerHelper\WaterFreeInstallerHelper.csproj"
& dotnet publish $helperProject -c $Configuration -r win-x64 --self-contained true /p:PublishSingleFile=true /p:DebugType=none /p:DebugSymbols=false -o $HelperOut
Assert-Exit "dotnet publish"

$helperExe = Join-Path $HelperOut "waterfree-installer-helper.exe"
if (-not (Test-Path $helperExe)) {
    throw "Helper build failed: $helperExe missing."
}
Copy-Item -LiteralPath $helperExe -Destination (Join-Path $PayloadDir "waterfree-installer-helper.exe") -Force
Write-Ok "Payload: waterfree-installer-helper.exe"

Write-Step "Building MSI with WiX..."
$msiOut = Join-Path $OutDir "WaterFreeSetup.msi"
& wix build $WxsPath -dPayloadDir="$PayloadDir" -dProductVersion="$ProductVersion" -out $msiOut
Assert-Exit "wix build"

Write-Ok "MSI built: $msiOut"
