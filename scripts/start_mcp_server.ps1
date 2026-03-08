[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Module,
    [string]$PythonPath = "python",
    [string]$WorkingDirectory = $PSScriptRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "waterfree-secrets.ps1")

$anthropic = Get-WaterFreeSecret -Name "ANTHROPIC_API_KEY"
if ($anthropic) {
    $env:ANTHROPIC_API_KEY = $anthropic
}

$openAi = Get-WaterFreeSecret -Name "OPENAI_API_KEY"
if ($openAi) {
    $env:OPENAI_API_KEY = $openAi
}

Set-Location $WorkingDirectory
& $PythonPath -m $Module
exit $LASTEXITCODE
