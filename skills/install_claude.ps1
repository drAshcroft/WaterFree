<#
.SYNOPSIS
    Install WaterFree skill packages into Claude Code's skills directory.

.DESCRIPTION
    Copies skill packages from this directory into ~/.claude/skills so that
    Claude Code picks them up automatically on next startup.

    Each skill is a sub-folder containing a SKILL.md file.

.PARAMETER SourceRoot
    Root of the skills directory. Defaults to the directory containing this script.

.PARAMETER Destination
    Target skills directory. Defaults to ~/.claude/skills.

.PARAMETER Skill
    One or more skill names to install (e.g. "waterfree-index", "waterfree-todos").
    Omit to install all available skills.

.PARAMETER IncludeOllamaSkills
    Install the Ollama-dependent skills (tutorialize, waterfree-qa-summary) even
    when no usable local Ollama model is detected. By default they are skipped
    on machines without a reasonable local model so they don't fail at runtime.

.EXAMPLE
    # Install all skills
    .\install_claude.ps1

.EXAMPLE
    # Install specific skills
    .\install_claude.ps1 -Skill waterfree-index, waterfree-todos

.EXAMPLE
    # Install to a custom path
    .\install_claude.ps1 -Destination C:\custom\path\skills
#>

param(
    [string]$SourceRoot  = $PSScriptRoot,
    [string]$Destination = (Join-Path $HOME ".claude\skills"),
    [string[]]$Skill,
    [switch]$IncludeOllamaSkills,
    [switch]$InstallHooks,
    [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Hold the window open on exit so errors are readable when double-clicked
# (skip when output is redirected / piped, or -NoPause is passed).
function Invoke-ExitPause {
    if ($NoPause -or $env:WATERFREE_NONINTERACTIVE) { return }
    try { if ([Console]::IsOutputRedirected -or [Console]::IsInputRedirected) { return } } catch { return }
    Write-Host ""
    Read-Host "Press Enter to close this window" | Out-Null
}
trap {
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Invoke-ExitPause
    exit 1
}

. (Join-Path $PSScriptRoot "_ollama_check.ps1")

$availablePackages = Get-ChildItem -Path $SourceRoot -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName "SKILL.md") } |
    Sort-Object Name

if (-not $availablePackages) {
    throw "No installable skill packages found under '$SourceRoot'."
}

# @(...) so a single-skill selection stays an array. Without it PowerShell
# unwraps to one DirectoryInfo, and $selectedPackages.Count below throws under
# Set-StrictMode -- installing exactly one skill would succeed, then error.
$selectedPackages = @(if ($Skill -and $Skill.Count -gt 0) {
    foreach ($s in $Skill) {
        $package = $availablePackages | Where-Object { $_.Name -eq $s }
        if (-not $package) {
            $known = ($availablePackages.Name | Sort-Object) -join ", "
            throw "Unknown skill '$s'. Available: $known"
        }
        $package
    }
} else {
    $availablePackages
})

# Gate the Ollama-dependent skills: skip them when there's no usable local
# model, unless the user explicitly named them or passed -IncludeOllamaSkills.
$explicitSelection = ($Skill -and $Skill.Count -gt 0)
if (-not $explicitSelection -and -not $IncludeOllamaSkills) {
    $ollamaPackages = @($selectedPackages | Where-Object { Test-PackageNeedsOllama $_ })
    if ($ollamaPackages.Count -gt 0) {
        $ollama = Test-OllamaReady
        if (-not ($ollama.Available -and $ollama.HasReasonableModel)) {
            $skippedNames = ($ollamaPackages.Name | Sort-Object) -join ", "
            Write-Host ""
            Write-Host "Skipping Ollama-dependent skill(s): $skippedNames" -ForegroundColor Yellow
            Write-Host "  Reason: $($ollama.Reason)" -ForegroundColor DarkYellow
            Write-Host "  Install a local model and pass -IncludeOllamaSkills to add them." -ForegroundColor DarkGray
            $skipNameSet = [System.Collections.Generic.HashSet[string]]::new(
                [string[]]@($ollamaPackages.Name), [System.StringComparer]::OrdinalIgnoreCase)
            $selectedPackages = @($selectedPackages | Where-Object { -not $skipNameSet.Contains($_.Name) })
        }
    }
}

if (-not $selectedPackages) {
    Write-Host ""
    Write-Host "No skills to install after dependency checks." -ForegroundColor Yellow
    Invoke-ExitPause
    return
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

foreach ($package in $selectedPackages) {
    $target = Join-Path $Destination $package.Name
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -Path (Join-Path $package.FullName "*") -Destination $target -Recurse -Force
    Write-Host "  Installed $($package.Name)  ->  $target"
}

if ($selectedPackages.Name -contains "waterfree-assets") {
    Write-Host ""
    Write-Host "Asset search includes local semantic modes:" -ForegroundColor Cyan
    Write-Host '  assetsearch.cmd "heavy metal scrape" --semantic-audio'
    Write-Host '  assetsearch.cmd "sword icon" --semantic-visual'
    Write-Host "  See waterfree-assets/SKILL.md for rebuild and model commands." -ForegroundColor DarkCyan
}

Write-Host ""
Write-Host "Installed $($selectedPackages.Count) skill(s) to $Destination"

if ($InstallHooks) {
    # Merge each installed skill's hooks/claude-settings.hooks.json into the
    # user's settings.json. Idempotent: a hook whose command already exists is
    # left alone, so re-running never duplicates entries.
    $settingsPath = Join-Path (Split-Path -Parent $Destination) "settings.json"
    $settings = @{}
    if (Test-Path $settingsPath) {
        $raw = Get-Content $settingsPath -Raw -Encoding UTF8
        if ($raw.Trim()) { $settings = $raw | ConvertFrom-Json }
    }
    $hooksTable = @{}
    if ($settings.PSObject.Properties["hooks"]) {
        foreach ($prop in $settings.hooks.PSObject.Properties) { $hooksTable[$prop.Name] = @($prop.Value) }
    }
    $added = 0
    foreach ($package in $selectedPackages) {
        $fragmentPath = Join-Path $package.FullName "hooks\claude-settings.hooks.json"
        if (-not (Test-Path $fragmentPath)) { continue }
        $fragment = Get-Content $fragmentPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($eventProp in $fragment.hooks.PSObject.Properties) {
            $eventName = $eventProp.Name
            $existing = @()
            if ($hooksTable.ContainsKey($eventName)) { $existing = @($hooksTable[$eventName]) }
            foreach ($group in @($eventProp.Value)) {
                $commands = @($group.hooks | ForEach-Object { $_.command })
                $present = $false
                foreach ($e in $existing) {
                    foreach ($h in @($e.hooks)) { if ($commands -contains $h.command) { $present = $true } }
                }
                if (-not $present) { $existing += $group; $added++ }
            }
            $hooksTable[$eventName] = $existing
        }
    }
    if ($settings -is [hashtable]) { $settings = [pscustomobject]$settings }
    if ($settings.PSObject.Properties["hooks"]) { $settings.hooks = [pscustomobject]$hooksTable }
    else { $settings | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]$hooksTable) }
    $settings | ConvertTo-Json -Depth 10 | Set-Content -Path $settingsPath -Encoding UTF8
    Write-Host "Merged $added hook(s) into $settingsPath"
}
Write-Host "Restart Claude Code to pick up new skills."
Invoke-ExitPause
