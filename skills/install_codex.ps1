param(
  [string]$SourceRoot = $PSScriptRoot,
  [string]$Destination = (Join-Path $HOME ".codex\skills"),
  [string[]]$Skill,
  [switch]$IncludeOllamaSkills,
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

New-Item -ItemType Directory -Force $Destination | Out-Null

foreach ($package in $selectedPackages) {
  $targetPath = Join-Path $Destination $package.Name
  New-Item -ItemType Directory -Force $targetPath | Out-Null
  Copy-Item -Path (Join-Path $package.FullName "*") -Destination $targetPath -Recurse -Force
  Write-Host "Installed $($package.Name) -> $targetPath"
}

Write-Host "Restart Codex to pick up new skills."
Invoke-ExitPause
