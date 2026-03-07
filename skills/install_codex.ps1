param(
  [string]$SourceRoot = $PSScriptRoot,
  [string]$Destination = (Join-Path $HOME ".codex\skills"),
  [string[]]$Skill
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$availablePackages = Get-ChildItem -Path $SourceRoot -Directory |
  Where-Object { Test-Path (Join-Path $_.FullName "SKILL.md") } |
  Sort-Object Name

if (-not $availablePackages) {
  throw "No installable skill packages were found under '$SourceRoot'."
}

$selectedPackages = if ($Skill -and $Skill.Count -gt 0) {
  foreach ($skillName in $Skill) {
    $package = $availablePackages | Where-Object { $_.Name -eq $skillName }
    if (-not $package) {
      $knownSkills = ($availablePackages.Name | Sort-Object) -join ", "
      throw "Unknown skill '$skillName'. Available skills: $knownSkills"
    }
    $package
  }
} else {
  $availablePackages
}

New-Item -ItemType Directory -Force $Destination | Out-Null

foreach ($package in $selectedPackages) {
  $targetPath = Join-Path $Destination $package.Name
  New-Item -ItemType Directory -Force $targetPath | Out-Null
  Copy-Item -Path (Join-Path $package.FullName "*") -Destination $targetPath -Recurse -Force
  Write-Host "Installed $($package.Name) -> $targetPath"
}

Write-Host "Restart Codex to pick up new skills."
