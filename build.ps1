<#
.SYNOPSIS
    Build the WaterFree backend into a self-contained executable.

.DESCRIPTION
    Uses PyInstaller to bundle the Python backend and all its dependencies
    into a single executable that needs no Python on the target machine.

    Output: bin/waterfree-<platform>-<arch>[.exe]

.PARAMETER PythonPath
    Path to a Python 3.10+ interpreter.  Defaults to auto-detection.

.PARAMETER Clean
    Delete the PyInstaller build/ cache before building.
#>

[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$ScriptDir = $PSScriptRoot

function Write-Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok([string]$m)   { Write-Host "    $m" -ForegroundColor Green }

function Assert-Exit([string]$cmd) {
    if ($LASTEXITCODE -ne 0) { throw "$cmd failed (exit $LASTEXITCODE)" }
}

# ---------------------------------------------------------------------------
# Find a working Python interpreter
# ---------------------------------------------------------------------------
function Find-Python {
    $candidates = [System.Collections.Generic.List[string]]::new()

    if ($PythonPath) { $candidates.Add($PythonPath) }

    # Windows py launcher
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $r = & $py.Source -3 -c "import sys; print(sys.executable)" 2>&1
            if ($LASTEXITCODE -eq 0 -and $r) { $candidates.Add($r.Trim()) }
        } catch { }
    }

    # Versioned install dirs
    $base = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $base) {
        Get-ChildItem $base -Filter "Python3*" -Directory |
            Sort-Object Name -Descending |
            ForEach-Object {
                $exe = Join-Path $_.FullName "python.exe"
                if (Test-Path $exe) { $candidates.Add($exe) }
            }
    }

    # PATH python (skip Windows Store stub)
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch "WindowsApps\\python") {
        $candidates.Add($cmd.Source)
    }

    foreach ($c in $candidates) {
        if (-not $c) { continue }
        try {
            $out = & $c -c "import sys; print(sys.executable)" 2>&1
            if ($LASTEXITCODE -eq 0 -and $out) { return $out.Trim() }
        } catch { }
    }

    throw "No working Python interpreter found. Pass -PythonPath explicitly."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
$python = Find-Python
Write-Ok "Using Python: $python"

# Ensure PyInstaller and all backend dependencies are installed
Write-Step "Installing build dependencies..."
& $python -m pip install --quiet pyinstaller -r (Join-Path $ScriptDir "backend\requirements.txt")
Assert-Exit "pip install"
Write-Ok "Dependencies ready."

if ($Clean -and (Test-Path (Join-Path $ScriptDir "build"))) {
    Write-Step "Cleaning build cache..."
    Remove-Item -LiteralPath (Join-Path $ScriptDir "build") -Recurse -Force
}

New-Item -ItemType Directory -Force -Path (Join-Path $ScriptDir "bin") | Out-Null

Write-Step "Running PyInstaller..."
Push-Location $ScriptDir
try {
    & $python -m PyInstaller waterfree.spec --noconfirm --distpath bin
    Assert-Exit "PyInstaller"
} finally {
    Pop-Location
}

# Find the built exe
$builtExe = Get-ChildItem (Join-Path $ScriptDir "bin") -Filter "waterfree-*" -File |
    Select-Object -First 1

if (-not $builtExe) {
    throw "Build succeeded but no executable found in bin/."
}

Write-Ok "Executable: $($builtExe.FullName)"
Write-Ok "Size: $([math]::Round($builtExe.Length / 1MB, 1)) MB"
Write-Host "`nBuild complete." -ForegroundColor Green
