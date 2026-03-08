[CmdletBinding()]
param(
    [ValidateSet("", "Read", "Write", "Has")]
    [string]$Action = "",
    [string]$Name = "",
    [string]$Secret = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-WaterFreeSecretStorePath {
    return Join-Path $HOME ".waterfree\secrets.json"
}

function Initialize-WaterFreeSecretStore {
    $storePath = Get-WaterFreeSecretStorePath
    $dir = Split-Path $storePath -Parent
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    if (-not (Test-Path $storePath)) {
        [PSCustomObject]@{
            version = 1
            secrets = [PSCustomObject]@{}
        } | ConvertTo-Json -Depth 5 | Set-Content -Path $storePath -Encoding UTF8
    }
}

function Get-WaterFreeSecretStore {
    Initialize-WaterFreeSecretStore
    $storePath = Get-WaterFreeSecretStorePath
    $raw = Get-Content -Path $storePath -Raw
    if (-not $raw.Trim()) {
        return [PSCustomObject]@{
            version = 1
            secrets = [PSCustomObject]@{}
        }
    }

    $parsed = $raw | ConvertFrom-Json
    if (-not ($parsed.PSObject.Properties.Name -contains "secrets")) {
        $parsed | Add-Member -MemberType NoteProperty -Name secrets -Value ([PSCustomObject]@{})
    }
    return $parsed
}

function Save-WaterFreeSecretStore($Store) {
    $storePath = Get-WaterFreeSecretStorePath
    $Store | ConvertTo-Json -Depth 5 | Set-Content -Path $storePath -Encoding UTF8
}

function Protect-WaterFreeSecret([string]$Value) {
    $secure = ConvertTo-SecureString -String $Value -AsPlainText -Force
    return ConvertFrom-SecureString -SecureString $secure
}

function Unprotect-WaterFreeSecret([string]$Value) {
    $secure = ConvertTo-SecureString -String $Value
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
}

function Set-WaterFreeSecret {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $store = Get-WaterFreeSecretStore
    if ($store.secrets.PSObject.Properties.Name -contains $Name) {
        $store.secrets.$Name = Protect-WaterFreeSecret -Value $Value
    } else {
        $store.secrets | Add-Member -MemberType NoteProperty -Name $Name -Value (Protect-WaterFreeSecret -Value $Value)
    }
    Save-WaterFreeSecretStore -Store $store
}

function Get-WaterFreeSecret {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $store = Get-WaterFreeSecretStore
    if (-not ($store.secrets.PSObject.Properties.Name -contains $Name)) {
        return $null
    }
    return Unprotect-WaterFreeSecret -Value ([string]$store.secrets.$Name)
}

function Test-WaterFreeSecret {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $store = Get-WaterFreeSecretStore
    return ($store.secrets.PSObject.Properties.Name -contains $Name)
}

if ($Action) {
    switch ($Action) {
        "Read" {
            if (-not $Name) {
                throw "Read requires -Name."
            }
            $value = Get-WaterFreeSecret -Name $Name
            if ($null -ne $value) {
                Write-Output $value
            }
        }
        "Write" {
            if (-not $Name) {
                throw "Write requires -Name."
            }
            Set-WaterFreeSecret -Name $Name -Value $Secret
        }
        "Has" {
            if (-not $Name) {
                throw "Has requires -Name."
            }
            if (Test-WaterFreeSecret -Name $Name) {
                Write-Output "true"
            } else {
                Write-Output "false"
            }
        }
    }
}
