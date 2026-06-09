<#
.SYNOPSIS
    Shared Ollama readiness probe for the skill installers.

.DESCRIPTION
    Dot-sourced by install_claude.ps1 and install_codex.ps1. Provides:

      Test-PackageNeedsOllama  -> $true if a skill package's SKILL.md declares
                                  an Ollama dependency.
      Test-OllamaReady         -> a result object describing whether a local
                                  Ollama daemon (or CLI) is reachable and has a
                                  reasonably-sized model installed.

    "Reasonable" means a model with >= 7B parameters (the QA-summary and
    tutorializer defaults are 7B-14B). Smaller models technically run but
    produce poor map/reduce and tutorial output, so they don't count.
#>

Set-StrictMode -Version Latest

# Minimum parameter count (in billions) for a model to be considered usable
# by the Ollama-backed skills.
$script:MinReasonableParamsB = 7.0

function Test-PackageNeedsOllama {
    param([Parameter(Mandatory)][System.IO.DirectoryInfo]$Package)

    $skillDoc = Join-Path $Package.FullName "SKILL.md"
    if (-not (Test-Path $skillDoc)) { return $false }
    return (Select-String -Path $skillDoc -Pattern "ollama" -SimpleMatch -Quiet)
}

function ConvertTo-ParamBillions {
    # Parses Ollama parameter_size strings like "14.8B", "7B", "8000M" into
    # a billions-of-parameters float. Returns $null when unparseable.
    param([string]$Raw)

    if (-not $Raw) { return $null }
    $value = $Raw.Trim()
    if ($value -match '^(?<num>[\d.]+)\s*(?<unit>[BMK])?$') {
        $num = [double]$Matches['num']
        switch ($Matches['unit']) {
            'B'     { return $num }
            'M'     { return $num / 1000.0 }
            'K'     { return $num / 1000000.0 }
            default { return $num }   # bare number — assume billions
        }
    }
    return $null
}

function Test-OllamaReady {
    <#
        Returns a PSCustomObject:
          Available          [bool]   daemon or CLI reachable
          HasReasonableModel [bool]   a >=7B model is installed
          Models             [string[]] model names found
          Base               [string] base URL probed
          Reason             [string] human-readable status
    #>
    $base = if ($env:WATERFREE_OLLAMA_BASE) { $env:WATERFREE_OLLAMA_BASE.TrimEnd('/') } else { "http://localhost:11434" }

    $result = [PSCustomObject]@{
        Available          = $false
        HasReasonableModel = $false
        Models             = @()
        Base               = $base
        Reason             = ""
    }

    # --- Path 1: REST daemon (gives us parameter sizes) ---------------------
    try {
        $tags = Invoke-RestMethod -Uri "$base/api/tags" -TimeoutSec 4 -ErrorAction Stop
        $result.Available = $true
        $models = @($tags.models)
        $result.Models = @($models | ForEach-Object { $_.name })

        foreach ($m in $models) {
            $paramsB = $null
            if ($m.PSObject.Properties.Name -contains 'details' -and $m.details) {
                $paramsB = ConvertTo-ParamBillions $m.details.parameter_size
            }
            # Fall back to on-disk size: ~>= 4GB is a 7B-class quantized model.
            if (($null -eq $paramsB) -and $m.size -and ($m.size -ge 4e9)) {
                $result.HasReasonableModel = $true
                break
            }
            if (($null -ne $paramsB) -and ($paramsB -ge $script:MinReasonableParamsB)) {
                $result.HasReasonableModel = $true
                break
            }
        }

        if (-not $result.Models) {
            $result.Reason = "Ollama daemon is running at $base but has no models installed."
        } elseif (-not $result.HasReasonableModel) {
            $result.Reason = "Ollama is running but only small models are installed ($($result.Models -join ', ')); need a >= ${script:MinReasonableParamsB}B model (e.g. 'ollama pull qwen2.5:14b')."
        } else {
            $result.Reason = "Ollama ready at $base with a usable model."
        }
        return $result
    } catch {
        # daemon not reachable — fall through to CLI probe
    }

    # --- Path 2: ollama CLI (daemon not running, but binary present) --------
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollama) {
        $result.Available = $true
        try {
            $listOutput = & $ollama.Source list 2>$null
            $names = @()
            foreach ($line in $listOutput) {
                if ($line -match '^\s*NAME' ) { continue }   # header
                $first = ($line -split '\s+', 2)[0]
                if ($first) { $names += $first }
            }
            $result.Models = $names
            if ($names.Count -gt 0) {
                # CLI listing doesn't expose parameter counts reliably; if any
                # model is present we trust it and let the daemon start on demand.
                $result.HasReasonableModel = $true
                $result.Reason = "Ollama CLI found with models ($($names -join ', ')); daemon will start on demand."
            } else {
                $result.Reason = "Ollama CLI is installed but no models are pulled (run 'ollama pull qwen2.5:14b')."
            }
        } catch {
            $result.Reason = "Ollama CLI found but 'ollama list' failed: $($_.Exception.Message)"
        }
        return $result
    }

    $result.Reason = "No Ollama daemon at $base and no 'ollama' binary on PATH. Install from https://ollama.com."
    return $result
}
