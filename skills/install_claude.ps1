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
    One or more skill names to install (e.g. "waterfree-index", "waterfree-debug").
    Omit to install all available skills.

.EXAMPLE
    # Install all skills
    .\install_claude.ps1

.EXAMPLE
    # Install specific skills
    .\install_claude.ps1 -Skill waterfree-index, waterfree-debug

.EXAMPLE
    # Install to a custom path
    .\install_claude.ps1 -Destination C:\custom\path\skills
#>

param(
    [string]$SourceRoot  = $PSScriptRoot,
    [string]$Destination = (Join-Path $HOME ".claude\skills"),
    [string[]]$Skill
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$availablePackages = Get-ChildItem -Path $SourceRoot -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName "SKILL.md") } |
    Sort-Object Name

if (-not $availablePackages) {
    throw "No installable skill packages found under '$SourceRoot'."
}

$selectedPackages = if ($Skill -and $Skill.Count -gt 0) {
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
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

foreach ($package in $selectedPackages) {
    $target = Join-Path $Destination $package.Name
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -Path (Join-Path $package.FullName "*") -Destination $target -Recurse -Force
    Write-Host "  Installed $($package.Name)  ->  $target"
}

Write-Host ""
Write-Host "Installed $($selectedPackages.Count) skill(s) to $Destination"
Write-Host "Restart Claude Code to pick up new skills."
