<#
.SYNOPSIS
    Builds the entire WaterFree installer from a clean checkout.

.DESCRIPTION
    Single entry point for the WaterFree release pipeline. Stages, in order:

      1. Launcher build     -> bin/waterfree-<plat>-<arch>\waterfree.exe
                              (a tiny .NET shim) + staged backend\ source
                              + bin/waterfree-runtime.zip
                              The shim shells into the shared venv at run time,
                              so only our own Python source ships (a few MB) —
                              not a 4+ GB frozen torch/transformers bundle.
      2. npm run package    -> waterfree.vsix
      3. WiX (dotnet build) -> dist/WaterFreeSetup-<version>.msi
                              (the WiX project's BuildPayload target internally
                               runs `dotnet publish` for the installer helper and
                               stages the backend runtime zip + vsix into its payload dir)

    Final artifacts are copied into ./dist at repo root:
      - WaterFreeSetup-<version>.msi
      - waterfree-<plat>-<arch>\waterfree.exe
      - waterfree-runtime.zip
      - waterfree.vsix

    Halts on the first failure with the failing command's exit code. Prints a
    `==> Stage N: <name>` banner per stage and a total wall-clock time at the
    end. Default invocation needs no arguments.

.PARAMETER ProductVersion
    Override the MSI's ProductVersion. Defaults to "0.1.0" (matches the wixproj).

.PARAMETER Clean
    Wipe bin/, dist/, the wix payload dir, and PyInstaller's build/ before
    starting. Useful when artifacts feel stale.

.PARAMETER SkipExe
    Reuse the existing bin/waterfree-<plat>-<arch>\ runtime instead of rebuilding
    the launcher and re-staging the backend source.

.PARAMETER SkipVsix
    Reuse an existing waterfree.vsix instead of running `npm run package`.

.PARAMETER SkipMsi
    Stop after the exe + vsix stages; do not build the MSI.

.PARAMETER SkipExeSmoke
    Skip the compiled executable smoke test after PyInstaller.

.PARAMETER SkipPrereqCheck
    Bypass the prerequisite probe (Python, PyInstaller, .NET SDK, Node, npm, WiX).
    Use only when you know exactly what's missing.

.EXAMPLE
    .\build_installer.ps1
    # Full build with defaults; outputs dist\WaterFreeSetup-0.1.0.msi

.EXAMPLE
    .\build_installer.ps1 -ProductVersion 0.2.0 -Clean
    # Clean build of version 0.2.0

.EXAMPLE
    .\build_installer.ps1 -SkipExe -SkipVsix
    # Just rebuild the MSI from the existing exe + vsix on disk
#>

[CmdletBinding()]
param(
    # Leave empty to auto-assign an ever-increasing 0.1.<build> version. A new
    # version is required on every build: the MSI's components key off registry
    # values, so without a MajorUpgrade (triggered by a higher version) Windows
    # Installer leaves ALL payload files stale on reinstall — the helper exe,
    # the runtime zip, skills, vsix. Pass an explicit value only for releases.
    [string]$ProductVersion = "",
    [switch]$Clean,
    [switch]$SkipExe,
    [switch]$SkipVsix,
    [switch]$SkipMsi,
    [switch]$SkipExeSmoke,
    [switch]$SkipPrereqCheck,
    [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$global:LASTEXITCODE = 0

# ---------------------------------------------------------------------------
# Keep the window open so failures are readable.
# When launched by double-click / "Run with PowerShell", the console closes the
# instant the script ends — a build error just flashes past. Pause before
# exiting unless output is redirected (CI / piped to a file) or -NoPause is set.
# ---------------------------------------------------------------------------
function Invoke-ExitPause {
    if ($NoPause -or $env:WATERFREE_NONINTERACTIVE) { return }
    try { if ([Console]::IsOutputRedirected -or [Console]::IsInputRedirected) { return } } catch { return }
    Write-Host ""
    Read-Host "Press Enter to close this window" | Out-Null
}

function Stop-Build([int]$code) {
    Invoke-ExitPause
    exit $code
}

# Assign a monotonically increasing 0.1.<build> version when one isn't given,
# so each build supersedes the last (MajorUpgrade) and refreshes every file.
# The counter lives outside the dirs wiped by -Clean so it never goes backward
# (a lower version would be blocked by AllowDowngrades=no).
function Resolve-ProductVersion([string]$requested, [string]$repoRoot) {
    if ($requested) { return $requested }
    $counterFile = Join-Path $repoRoot "installer\.build-number"
    $n = 0
    if (Test-Path $counterFile) {
        [int]::TryParse((Get-Content $counterFile -Raw).Trim(), [ref]$n) | Out-Null
    }
    $n = ($n + 1)
    if ($n -gt 65535) { $n = 1 }   # MSI version field max
    Set-Content -Path $counterFile -Value $n -Encoding ascii
    return "0.1.$n"
}

# Catch any terminating error ($ErrorActionPreference = Stop) so it is shown and
# the window held open instead of vanishing.
trap {
    Write-Host ""
    Write-Host "BUILD ERROR: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ScriptStackTrace) { Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray }
    Invoke-ExitPause
    exit 1
}

$RepoRoot   = $PSScriptRoot
$ProductVersion = Resolve-ProductVersion $ProductVersion $RepoRoot
Write-Host "==> Building WaterFree $ProductVersion" -ForegroundColor Cyan
$DistDir    = Join-Path $RepoRoot "dist"
$BinDir     = Join-Path $RepoRoot "bin"
$WixDir     = Join-Path $RepoRoot "installer"
$WixProj    = Join-Path $WixDir   "WaterFreeInstaller.wixproj"
$WixPayload = Join-Path $WixDir   "obj\payload"
$BuildDir   = Join-Path $RepoRoot "build"   # build scratch (cleaned by -Clean)
$VsixPath   = Join-Path $RepoRoot "waterfree.vsix"

# Runtime naming: waterfree-<sys.platform>-<arch>/waterfree(.exe). Mirrors the
# path the VS Code extension probes (PythonBridge.start).
$PlatformTag = switch -Regex ($env:OS) {
    "Windows" { "win32" }
    default   { "linux" }
}
$ArchTag = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
$RuntimeDirName = "waterfree-$PlatformTag-$ArchTag"
$LauncherName = if ($PlatformTag -eq "win32") { "waterfree.exe" } else { "waterfree" }
$RuntimeZipName = "waterfree-runtime.zip"
$RuntimeDirPath = Join-Path $BinDir $RuntimeDirName
$ExePath = Join-Path $RuntimeDirPath $LauncherName
$RuntimeZipPath = Join-Path $BinDir $RuntimeZipName

# The launcher is a tiny .NET shim that shells into a Python interpreter at run
# time. WATERFREE_PYTHON overrides; the default is the shared dev/internal venv.
$VenvPython         = if ($env:WATERFREE_PYTHON) { $env:WATERFREE_PYTHON } else { "C:\Projects\.local\Scripts\python.exe" }
$LauncherProj       = Join-Path $RepoRoot "installer\WaterFreeLauncher\WaterFreeLauncher.csproj"
$LauncherPublishDir = Join-Path $RepoRoot "installer\WaterFreeLauncher\publish"
$BackendSrc         = Join-Path $RepoRoot "backend"

$Banner = "==>"
$Start  = Get-Date

function Write-Stage([string]$label) {
    Write-Host ""
    Write-Host "$Banner $label" -ForegroundColor Cyan
}

function Invoke-Stage([string]$name, [scriptblock]$body) {
    Write-Stage $name
    $stageStart = Get-Date
    $global:LASTEXITCODE = 0
    & $body
    $code = $global:LASTEXITCODE
    if ($code -and $code -ne 0) {
        Write-Host "FAILED ($name) exit=$code" -ForegroundColor Red
        Stop-Build $code
    }
    $elapsed = (Get-Date) - $stageStart
    Write-Host ("    ({0:n1}s)" -f $elapsed.TotalSeconds) -ForegroundColor DarkGray
}

function Test-Command([string]$name) {
    $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

function Test-PythonModule([string]$module) {
    # Run with errors non-terminating: on PowerShell 7+ a native command that
    # writes to stderr can otherwise throw under $ErrorActionPreference=Stop,
    # turning "module not installed" into an unhandled error.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & python -c "import $module" 1>$null 2>$null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $prev
    }
}

# ---------------------------------------------------------------------------
# Stage 0: Prerequisite probe
# ---------------------------------------------------------------------------
if (-not $SkipPrereqCheck) {
    Invoke-Stage "Stage 0: Verifying prerequisites" {
        $missing = @()

        if (-not $SkipExe) {
            # The launcher is built with `dotnet publish` (no Python needed to build it).
            if (-not (Test-Command "dotnet")) {
                $missing += ".NET SDK 10 (install via winget install Microsoft.DotNet.SDK.10)"
            }
            # The exe smoke test runs the launcher, which shells into the venv.
            if (-not $SkipExeSmoke -and -not (Test-Path $VenvPython)) {
                $missing += "Shared venv Python at '$VenvPython' (the launcher shells into it; set WATERFREE_PYTHON or create the venv). Needed for the exe smoke test."
            }
        }

        if (-not $SkipVsix) {
            if (-not (Test-Command "node")) { $missing += "Node.js 18+ (install from nodejs.org or via winget install OpenJS.NodeJS.LTS)" }
            if (-not (Test-Command "npm"))  { $missing += "npm (ships with Node.js)" }
        }

        if (-not $SkipMsi) {
            if (-not (Test-Command "dotnet")) {
                $missing += ".NET SDK 10 (install via winget install Microsoft.DotNet.SDK.10)"
            }
            # WiX 4 SDK is restored via NuGet from the wixproj, so no separate
            # tool needs to be on PATH. We do need `dotnet build` to be able to
            # restore NuGet packages — that comes with the .NET SDK above.
        }

        if ($missing.Count -gt 0) {
            Write-Host ""
            Write-Host "Missing prerequisites:" -ForegroundColor Red
            foreach ($m in ($missing | Select-Object -Unique)) { Write-Host "  - $m" -ForegroundColor Red }
            Write-Host ""
            Write-Host "Install the items above, then re-run build_installer.ps1." -ForegroundColor Yellow
            Write-Host "(To bypass this check, pass -SkipPrereqCheck.)" -ForegroundColor DarkGray
            Stop-Build 2
        }
        Write-Host "    All required toolchains present." -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# Optional: clean previous artifacts
# ---------------------------------------------------------------------------
if ($Clean) {
    Invoke-Stage "Stage 0.5: Cleaning previous artifacts" {
        foreach ($p in @($DistDir, $BinDir, $WixPayload, $BuildDir)) {
            if (Test-Path $p) {
                Remove-Item -Recurse -Force -Path $p
                Write-Host "    Removed $p" -ForegroundColor DarkGray
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Stage 1: PyInstaller -> bin/waterfree-<plat>-<arch>/ + runtime zip
# ---------------------------------------------------------------------------
if ($SkipExe) {
    Write-Stage "Stage 1: PyInstaller (skipped, -SkipExe)"
    if (-not (Test-Path $ExePath)) {
        Write-Host "FAILED: -SkipExe was passed but $ExePath does not exist." -ForegroundColor Red
        Stop-Build 3
    }
    if (-not (Test-Path $RuntimeZipPath)) {
        Write-Host "FAILED: -SkipExe was passed but $RuntimeZipPath does not exist." -ForegroundColor Red
        Stop-Build 3
    }
} else {
    Invoke-Stage "Stage 1: Launcher + backend -> $RuntimeDirName" {
        New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

        # Start from a clean runtime dir so stale files never leak into the zip.
        if (Test-Path $RuntimeDirPath) {
            Remove-Item -Recurse -Force -Path $RuntimeDirPath
        }
        New-Item -ItemType Directory -Force -Path $RuntimeDirPath | Out-Null

        # 1) Build the tiny native launcher (waterfree.exe). Framework-dependent
        #    single file -> a few hundred KB, not a frozen Python runtime.
        if (Test-Path $LauncherPublishDir) {
            Remove-Item -Recurse -Force -Path $LauncherPublishDir
        }
        & dotnet publish $LauncherProj -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -o $LauncherPublishDir
        if ($LASTEXITCODE -ne 0) {
            Write-Host "FAILED: dotnet publish of the launcher returned $LASTEXITCODE." -ForegroundColor Red
            Stop-Build $LASTEXITCODE
        }
        $publishedExe = Join-Path $LauncherPublishDir $LauncherName
        if (-not (Test-Path $publishedExe)) {
            Write-Host "FAILED: launcher publish succeeded but $publishedExe is missing." -ForegroundColor Red
            Stop-Build 1
        }
        # Copy the launcher (exe + any runtimeconfig/deps json) — never the pdb.
        Get-ChildItem -Path $LauncherPublishDir -File |
            Where-Object { $_.Extension -ne ".pdb" } |
            ForEach-Object { Copy-Item -Force $_.FullName (Join-Path $RuntimeDirPath $_.Name) }

        # 2) Stage the pure-Python backend source next to the launcher. The heavy
        #    third-party deps come from the shared venv at run time, so only our
        #    own source ships — a few MB instead of 4+ GB.
        $backendDest = Join-Path $RuntimeDirPath "backend"
        Copy-Item -Recurse -Force $BackendSrc $backendDest
        # Drop test trees and stale bytecode from the shipped copy (deepest first).
        Get-ChildItem -Path $backendDest -Recurse -Directory -Force |
            Where-Object { $_.Name -in @("__pycache__", "tests", ".pytest_cache") } |
            Sort-Object FullName -Descending |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Get-ChildItem -Path $backendDest -Recurse -File -Force -Filter *.pyc |
            Remove-Item -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path (Join-Path $backendDest "main.py"))) {
            Write-Host "FAILED: backend\main.py missing from staged runtime ($backendDest)." -ForegroundColor Red
            Stop-Build 1
        }

        # 3) Zip the runtime contents (not the dir) for the MSI helper to extract
        #    straight into %LocalAppData%\WaterFree\bin\.
        if (Test-Path $RuntimeZipPath) {
            Remove-Item -Force -Path $RuntimeZipPath
        }
        Compress-Archive -Path (Join-Path $RuntimeDirPath "*") -DestinationPath $RuntimeZipPath -Force
        Write-Host "    -> $RuntimeDirPath" -ForegroundColor DarkGray
        Write-Host "    -> $RuntimeZipPath" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# Stage 1.5: Compiled executable smoke test
# ---------------------------------------------------------------------------
if ($SkipExeSmoke) {
    Write-Stage "Stage 1.5: Compiled exe smoke test (skipped, -SkipExeSmoke)"
} else {
    Invoke-Stage "Stage 1.5: Smoke test compiled exe" {
        Push-Location $RepoRoot
        try {
            # Dependency self-check first: a bundle missing required Python
            # packages (e.g. networkx, tree-sitter grammars) imports fine but
            # degrades at runtime. `doctor` exits non-zero so we never ship one.
            & $ExePath doctor
            if ($LASTEXITCODE -ne 0) {
                Write-Host "FAILED: 'waterfree doctor' reported missing runtime dependencies (exit=$LASTEXITCODE)." -ForegroundColor Red
                Write-Host "The shared venv ($VenvPython) is missing packages from backend/requirements.txt." -ForegroundColor Yellow
                Stop-Build $LASTEXITCODE
            }
            # Run the smoke harness with the venv interpreter — bare `python` may
            # not be on PATH, and the harness only needs stdlib to drive the exe.
            & $VenvPython -m backend.tests.smoke_compiled_exe --exe $ExePath
        } finally {
            Pop-Location
        }
    }
}

# ---------------------------------------------------------------------------
# Stage 2: npm run package -> waterfree.vsix
# ---------------------------------------------------------------------------
if ($SkipVsix) {
    Write-Stage "Stage 2: npm run package (skipped, -SkipVsix)"
    if (-not (Test-Path $VsixPath)) {
        Write-Host "FAILED: -SkipVsix was passed but $VsixPath does not exist." -ForegroundColor Red
        Stop-Build 3
    }
} else {
    Invoke-Stage "Stage 2: npm run package -> waterfree.vsix" {
        Push-Location $RepoRoot
        try {
            & npm run package
        } finally {
            Pop-Location
        }
    }
    if (-not (Test-Path $VsixPath)) {
        Write-Host "FAILED: npm run package succeeded but $VsixPath is missing." -ForegroundColor Red
        Stop-Build 1
    }
}

# ---------------------------------------------------------------------------
# Stage 3: WiX -> dist/WaterFreeSetup-<version>.msi
# ---------------------------------------------------------------------------
if ($SkipMsi) {
    Write-Stage "Stage 3: WiX (skipped, -SkipMsi)"
    Write-Host "    Built exe + vsix only; MSI not produced."
} else {
    Invoke-Stage "Stage 3: WiX -> WaterFreeSetup.msi" {
        Push-Location $WixDir
        try {
            & dotnet build $WixProj -c Release -p:ProductVersion=$ProductVersion
        } finally {
            Pop-Location
        }
    }

    # WiX writes into installer/bin/<Config>/<TargetFramework or empty>/
    $msiCandidate = Get-ChildItem -Path (Join-Path $WixDir "bin") -Recurse -Filter "WaterFreeSetup*.msi" -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime -Descending |
                    Select-Object -First 1
    if (-not $msiCandidate) {
        Write-Host "FAILED: WiX build finished but no MSI was found under $WixDir\bin." -ForegroundColor Red
        Stop-Build 1
    }
}

# ---------------------------------------------------------------------------
# Stage 4: Stage artifacts into dist/
# ---------------------------------------------------------------------------
Invoke-Stage "Stage 4: Staging artifacts into dist/" {
    New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
    if (Test-Path (Join-Path $DistDir $RuntimeDirName)) {
        Remove-Item -Recurse -Force -Path (Join-Path $DistDir $RuntimeDirName)
    }
    Copy-Item -Recurse -Force $RuntimeDirPath (Join-Path $DistDir $RuntimeDirName)
    Copy-Item -Force $RuntimeZipPath (Join-Path $DistDir $RuntimeZipName)
    Copy-Item -Force $VsixPath (Join-Path $DistDir "waterfree.vsix")
    if (-not $SkipMsi) {
        $finalMsiName = "WaterFreeSetup-$ProductVersion.msi"
        Copy-Item -Force $msiCandidate.FullName (Join-Path $DistDir $finalMsiName)
        Write-Host "    -> dist\$finalMsiName" -ForegroundColor Green
    }
    Write-Host "    -> dist\$RuntimeDirName\"   -ForegroundColor Green
    Write-Host "    -> dist\$RuntimeZipName"    -ForegroundColor Green
    Write-Host "    -> dist\waterfree.vsix"    -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
$total = (Get-Date) - $Start
Write-Host ""
Write-Host ("==> Build complete in {0:n1}s" -f $total.TotalSeconds) -ForegroundColor Green
Write-Host "    Artifacts: $DistDir" -ForegroundColor Green
Invoke-ExitPause
