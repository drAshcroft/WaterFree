<# Keep the validated current installer and remove superseded staged MSIs. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DistDirectory,
    [Parameter(Mandatory)][string]$CurrentMsi
)

$ErrorActionPreference = 'Stop'
$installerDirectory = (Resolve-Path -LiteralPath $DistDirectory).ProviderPath
$currentInstaller = Get-Item -LiteralPath $CurrentMsi
if ($currentInstaller.PSIsContainer -or $currentInstaller.Length -eq 0 -or
    $currentInstaller.Name -notlike 'WaterFreeSetup-*.msi' -or
    -not [string]::Equals($currentInstaller.DirectoryName, $installerDirectory, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The current installer must be a nonempty WaterFreeSetup-*.msi inside the staging directory.'
}

$removedInstallers = 0
foreach ($supersededInstaller in Get-ChildItem -LiteralPath $installerDirectory -File -Filter 'WaterFreeSetup-*.msi') {
    if ([string]::Equals($supersededInstaller.FullName, $currentInstaller.FullName, [StringComparison]::OrdinalIgnoreCase)) { continue }
    if (-not [string]::Equals($supersededInstaller.DirectoryName, $installerDirectory, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installer cleanup target escaped the staging directory.'
    }
    Remove-Item -LiteralPath $supersededInstaller.FullName -Force
    $removedInstallers++
}
Write-Host "    Retained $($currentInstaller.Name); removed $removedInstallers superseded installer(s)."
