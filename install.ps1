<#
.SYNOPSIS
    Interactive installer for WaterFree.
#>

[CmdletBinding()]
param(
    [switch]$Quick,
    [string]$PythonPath = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot

. (Join-Path $ScriptDir "scripts\waterfree-secrets.ps1")

function Read-PlainSecret([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
}

function Confirm-Step([string]$Prompt, [bool]$Default = $true) {
    $suffix = if ($Default) { "[Y/n]" } else { "[y/N]" }
    $answer = Read-Host "$Prompt $suffix"
    if (-not $answer.Trim()) {
        return $Default
    }
    return $answer.Trim().ToLowerInvariant() -in @("y", "yes")
}

function Invoke-DeployTarget([string]$TargetName) {
    & (Join-Path $ScriptDir "deploy.ps1") $TargetName -Quick:$Quick -PythonPath $PythonPath
}

Write-Host "WaterFree Installer" -ForegroundColor Cyan
Write-Host "This installer can install the VS Code extension, optional MCP integrations, and store provider keys using Windows DPAPI." -ForegroundColor DarkGray

$installLocal = Confirm-Step -Prompt "Install the VS Code extension?" -Default $true
$installClaude = Confirm-Step -Prompt "Install MCP servers and skills into Claude Code?" -Default $false
$installCodex = Confirm-Step -Prompt "Install MCP servers and skills into Codex?" -Default $false
$installKilo = Confirm-Step -Prompt "Install MCP servers into Kilo Code?" -Default $false

if (Confirm-Step -Prompt "Store or update your Anthropic API key securely?" -Default $true) {
    $anthropic = Read-PlainSecret -Prompt "Anthropic API key"
    if ($anthropic.Trim()) {
        Set-WaterFreeSecret -Name "ANTHROPIC_API_KEY" -Value $anthropic.Trim()
        Write-Host "  Saved ANTHROPIC_API_KEY to the DPAPI-protected WaterFree secret store." -ForegroundColor Green
    }
}

if (Confirm-Step -Prompt "Store or update your OpenAI API key securely?" -Default $false) {
    $openAi = Read-PlainSecret -Prompt "OpenAI API key"
    if ($openAi.Trim()) {
        Set-WaterFreeSecret -Name "OPENAI_API_KEY" -Value $openAi.Trim()
        Write-Host "  Saved OPENAI_API_KEY to the DPAPI-protected WaterFree secret store." -ForegroundColor Green
    }
}

if ($installLocal) {
    Invoke-DeployTarget -TargetName "local"
}
if ($installClaude) {
    Invoke-DeployTarget -TargetName "claude"
}
if ($installCodex) {
    Invoke-DeployTarget -TargetName "codex"
}
if ($installKilo) {
    Invoke-DeployTarget -TargetName "kilo"
}

Write-Host "`nInstaller complete." -ForegroundColor Green
Write-Host "If this is your first VS Code install, run 'WaterFree: Setup' once so the extension copies the stored Anthropic key into VS Code secret storage." -ForegroundColor DarkGray
