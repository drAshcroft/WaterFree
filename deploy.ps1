<#
.SYNOPSIS
    Deploy the WaterFree VS Code extension and/or MCP integrations.

.DESCRIPTION
    Targets:
      local       - Build VSIX and install to VS Code
      claude      - Install skills + register MCP servers in Claude Code
      codex       - Install skills + register MCP servers in Codex
      kilo        - Register MCP servers in Kilo Code
      interactive - Launch the guided installer
      all         - local + claude + codex + kilo
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("local", "claude", "codex", "kilo", "interactive", "all")]
    [string]$Target = "local",

    [switch]$Quick,

    [string]$PythonPath = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$ScriptDir = $PSScriptRoot
$SkillsDir = Join-Path $ScriptDir "skills"
$VsixPath = Join-Path $ScriptDir "waterfree.vsix"

$CodeCmd = @(
    "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd",
    "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd",
    (Get-Command code.cmd -ErrorAction SilentlyContinue)?.Source
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $CodeCmd) {
    $CodeCmd = "code"
}

$McpServers = @(
    @{ name = "waterfree-index"; module = "backend.mcp_index" }
    @{ name = "waterfree-knowledge"; module = "backend.mcp_knowledge" }
    @{ name = "waterfree-todos"; module = "backend.mcp_todos" }
    @{ name = "waterfree-testing"; module = "backend.mcp_testing" }
)

$LegacyMcpServerNames = @(
    "pairprogram-debug",
    "pairprogram-index",
    "pairprogram-knowledge",
    "pairprogram-todos"
)

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "    $Message" -ForegroundColor Green
}

function Assert-LastExitCode([string]$CommandName) {
    if ($LASTEXITCODE -ne 0) {
        throw "$CommandName failed with exit code $LASTEXITCODE."
    }
}

function Get-ArtifactInfo([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path $Path)) {
        return $null
    }

    $item = Get-Item $Path
    $hash = (Get-FileHash -Path $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    return [PSCustomObject]@{
        Path          = $item.FullName
        LastWriteTime = $item.LastWriteTime
        Length        = $item.Length
        Sha256        = $hash.Substring(0, 12)
    }
}

function Write-ArtifactInfo([string]$Label, $Info) {
    if ($null -eq $Info) {
        Write-Host "    ${Label}: missing" -ForegroundColor Yellow
        return
    }

    $timestamp = $Info.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
    Write-Ok "${Label}: $($Info.Path)"
    Write-Host "    modified=$timestamp size=$($Info.Length) sha256=$($Info.Sha256)"
}

function Get-InstalledExtensionPath([string]$ExtensionId) {
    $extensionsRoot = Join-Path $HOME ".vscode\extensions"
    if (-not (Test-Path $extensionsRoot)) {
        return $null
    }

    $installDir = Get-ChildItem -Path $extensionsRoot -Directory |
        Where-Object { $_.Name -like "$ExtensionId*" } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $installDir) {
        return $null
    }

    return $installDir.FullName
}

function Get-InstalledExtensionBundlePath([string]$ExtensionId) {
    $installPath = Get-InstalledExtensionPath -ExtensionId $ExtensionId
    if ([string]::IsNullOrWhiteSpace($installPath)) {
        return $null
    }

    $bundlePath = Join-Path $installPath "dist\extension.js"
    if (Test-Path $bundlePath) {
        return $bundlePath
    }

    return $null
}

function Get-InstalledExtensionAssetPath([string]$ExtensionId, [string]$RelativePath) {
    $installPath = Get-InstalledExtensionPath -ExtensionId $ExtensionId
    if ([string]::IsNullOrWhiteSpace($installPath)) {
        return $null
    }

    $assetPath = Join-Path $installPath $RelativePath
    if (Test-Path $assetPath) {
        return $assetPath
    }

    return $null
}

function New-McpEntry([string]$Module) {
    return [ordered]@{
        command = $PythonPath
        args    = @("-m", $Module)
        env     = [ordered]@{ PYTHONPATH = $ScriptDir }
    }
}

function ConvertTo-McpServerMap($Value) {
    $map = [ordered]@{}
    if ($null -eq $Value) {
        return $map
    }

    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string] -and $Value.PSObject.Properties.Name -notcontains "Name") {
        foreach ($item in @($Value)) {
            if ($null -eq $item) {
                continue
            }
            $nameProperty = $item.PSObject.Properties["name"]
            if ($null -eq $nameProperty) {
                continue
            }
            if ([string]::IsNullOrWhiteSpace([string]$nameProperty.Value)) {
                continue
            }

            $name = [string]$nameProperty.Value
            $entry = [ordered]@{}
            foreach ($prop in $item.PSObject.Properties) {
                if ($prop.Name -eq "name") {
                    continue
                }
                $entry[$prop.Name] = $prop.Value
            }
            $map[$name] = $entry
        }
        return $map
    }

    foreach ($prop in $Value.PSObject.Properties) {
        $map[$prop.Name] = $prop.Value
    }
    return $map
}

function ConvertFrom-McpServerMap($Map, [bool]$AsArray) {
    if ($AsArray) {
        $items = New-Object System.Collections.Generic.List[object]
        foreach ($name in $Map.Keys) {
            $entry = [ordered]@{ name = $name }
            $value = $Map[$name]
            foreach ($prop in $value.PSObject.Properties) {
                $entry[$prop.Name] = $prop.Value
            }
            $items.Add([PSCustomObject]$entry)
        }
        return @($items.ToArray())
    }

    $obj = [PSCustomObject]@{}
    foreach ($name in $Map.Keys) {
        $obj | Add-Member -MemberType NoteProperty -Name $name -Value $Map[$name]
    }
    return $obj
}

function Merge-JsonMcpConfig([string]$ConfigPath) {
    $config = if (Test-Path $ConfigPath) {
        Get-Content $ConfigPath -Raw | ConvertFrom-Json
    } else {
        [PSCustomObject]@{}
    }

    $existingMcpServers = if ($config.PSObject.Properties.Name -contains "mcpServers") {
        $config.mcpServers
    } else {
        $null
    }
    $mcpServersAsArray = $existingMcpServers -is [System.Collections.IEnumerable] -and
        $existingMcpServers -isnot [string] -and
        $existingMcpServers.PSObject.Properties.Name -notcontains "Name"
    $mcpServerMap = ConvertTo-McpServerMap -Value $existingMcpServers

    foreach ($legacyName in $LegacyMcpServerNames) {
        if ($mcpServerMap.Contains($legacyName)) {
            $mcpServerMap.Remove($legacyName)
            Write-Host "    Removed legacy $legacyName"
        }
    }

    foreach ($server in $McpServers) {
        $entry = New-McpEntry -Module $server.module
        $mcpServerMap[$server.name] = [PSCustomObject]$entry
        Write-Host "    Registered $($server.name)"
    }

    $normalizedMcpServers = ConvertFrom-McpServerMap -Map $mcpServerMap -AsArray:$mcpServersAsArray
    if ($config.PSObject.Properties.Name -contains "mcpServers") {
        $config.mcpServers = $normalizedMcpServers
    } else {
        $config | Add-Member -MemberType NoteProperty -Name mcpServers -Value $normalizedMcpServers
    }

    $dir = Split-Path $ConfigPath -Parent
    if ($dir) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $config | ConvertTo-Json -Depth 10 | Set-Content -Path $ConfigPath -Encoding UTF8
    Write-Ok "Config written: $ConfigPath"
}





function Deploy-Local {
    $manifest = Get-Content (Join-Path $ScriptDir "package.json") -Raw | ConvertFrom-Json
    $extensionId = "$($manifest.publisher).$($manifest.name)"
    $extensionVersion = [string]$manifest.version
    $bundlePath = Join-Path $ScriptDir "dist\extension.js"

    Write-Step "Building extension $extensionId@$extensionVersion..."
    Push-Location $ScriptDir
    try {
        if ($Quick) {
            npm run compile
            Assert-LastExitCode "npm run compile"
        } else {
            npm run vscode:prepublish
            Assert-LastExitCode "npm run vscode:prepublish"
        }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path $bundlePath)) {
        throw "Build did not create $bundlePath."
    }

    $bundleInfo = Get-ArtifactInfo -Path $bundlePath
    Write-ArtifactInfo -Label "Built bundle" -Info $bundleInfo

    Write-Step "Packaging VSIX..."
    if (Test-Path $VsixPath) {
        Remove-Item $VsixPath -Force
    }

    Push-Location $ScriptDir
    try {
        npx --yes @vscode/vsce package --no-dependencies --out $VsixPath --baseContentUrl https://localhost
        Assert-LastExitCode "VSIX packaging"
    } finally {
        Pop-Location
    }

    if (-not (Test-Path $VsixPath)) {
        throw "VSIX packaging did not create $VsixPath."
    }

    $vsixInfo = Get-ArtifactInfo -Path $VsixPath
    Write-ArtifactInfo -Label "Built VSIX" -Info $vsixInfo

    Write-Step "Installing extension $extensionId@$extensionVersion..."
    $installOut = & $CodeCmd --install-extension $VsixPath --force 2>&1
    if ($LASTEXITCODE -ne 0 -and ($installOut -match "EBUSY|restart VS Code")) {
        Write-Host "    Extension is locked by a running VS Code instance." -ForegroundColor Yellow
        Write-Host "    Close VS Code or run 'Developer: Reload Window', then re-run the installer." -ForegroundColor Yellow
    } elseif ($LASTEXITCODE -ne 0) {
        throw "VS Code extension install failed with exit code $LASTEXITCODE.`n$installOut"
    } else {
        Write-Host $installOut
        $installedExtensionLine = & $CodeCmd --list-extensions --show-versions 2>$null |
            Where-Object { $_ -like "$extensionId@*" } |
            Select-Object -First 1
        if ($LASTEXITCODE -eq 0 -and $installedExtensionLine) {
            Write-Ok "VS Code reports installed extension: $installedExtensionLine"
        }

        $installedBundlePath = Get-InstalledExtensionBundlePath -ExtensionId $extensionId
        $installedBundleInfo = Get-ArtifactInfo -Path $installedBundlePath
        Write-ArtifactInfo -Label "Installed bundle" -Info $installedBundleInfo
        if ($installedBundleInfo -and $bundleInfo -and $installedBundleInfo.Sha256 -eq $bundleInfo.Sha256) {
            Write-Ok "Installed bundle matches freshly built bundle."
        } elseif ($installedBundleInfo -and $bundleInfo) {
            Write-Warning "Installed bundle hash does not match the freshly built bundle."
        } else {
            Write-Warning "Could not verify the installed bundle contents."
        }

        $repoSidebarAssetPath = Join-Path $ScriptDir "src\ui\sidebar\sidebar.html"
        $installedSidebarAssetPath = Get-InstalledExtensionAssetPath -ExtensionId $extensionId -RelativePath "src\ui\sidebar\sidebar.html"
        $repoSidebarInfo = Get-ArtifactInfo -Path $repoSidebarAssetPath
        $installedSidebarInfo = Get-ArtifactInfo -Path $installedSidebarAssetPath
        Write-ArtifactInfo -Label "Installed sidebar asset" -Info $installedSidebarInfo
        if ($installedSidebarInfo -and $repoSidebarInfo -and $installedSidebarInfo.Sha256 -eq $repoSidebarInfo.Sha256) {
            Write-Ok "Installed sidebar asset matches repo asset."
        } elseif ($installedSidebarInfo -and $repoSidebarInfo) {
            Write-Warning "Installed sidebar asset does not match the repo asset."
        } else {
            Write-Warning "Could not verify the installed sidebar asset."
        }

        Write-Ok "Installed. Use 'Developer: Reload Window' in VS Code to activate."
    }
}

function Deploy-Claude {
    Write-Step "Installing skills for Claude Code..."
    & (Join-Path $SkillsDir "install_claude.ps1")

    Write-Step "Registering MCP servers in Claude Code (--scope user)..."

    foreach ($legacyName in $LegacyMcpServerNames) {
        $out = claude mcp remove --scope user $legacyName 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    Removed legacy $legacyName"
        }
    }

    foreach ($server in $McpServers) {
        # Remove first so re-running is idempotent
        claude mcp remove --scope user $server.name 2>&1 | Out-Null

        $addArgs = @('mcp', 'add', '-s', 'user', '-e', "PYTHONPATH=$ScriptDir", '--', $server.name, $PythonPath, '-m', $server.module)
        $out = & claude @addArgs 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    Registered $($server.name)"
        } else {
            Write-Warning "    Failed to register $($server.name): $out"
        }
    }

    Write-Ok "Reload the VSCode window (Developer: Reload Window) to activate MCP servers."
}

function Deploy-Codex {
    Write-Step "Installing skills for Codex..."
    & (Join-Path $SkillsDir "install_codex.ps1")

    Write-Step "Registering MCP servers in Codex (~/.codex/config.toml via codex mcp)..."

    foreach ($legacyName in $LegacyMcpServerNames) {
        $out = & codex mcp remove $legacyName 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    Removed legacy $legacyName"
        }
    }

    foreach ($server in $McpServers) {
        # Remove first so re-running is idempotent.
        & codex mcp remove $server.name 2>&1 | Out-Null

        $addArgs = @('mcp', 'add', $server.name, '--env', "PYTHONPATH=$ScriptDir", '--', $PythonPath, '-m', $server.module)
        $out = & codex @addArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "    Failed to register $($server.name): $out"
            continue
        }

        $verifyOut = & codex mcp get $server.name --json 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    Registered $($server.name)"
        } else {
            Write-Warning "    Registered $($server.name), but verification failed: $verifyOut"
        }
    }

    Write-Ok "Restart Codex to activate MCP servers."
}

function Deploy-Kilo {
    $kiloConfigPath = Join-Path $env:APPDATA "Kilo Code\User\globalStorage\kilocode.kilo-code\settings\mcp_settings.json"
    Write-Step "Registering MCP servers in Kilo Code ($kiloConfigPath)..."
    Merge-JsonMcpConfig -ConfigPath $kiloConfigPath

    Write-Ok "Restart Kilo Code to activate MCP servers."
}

function Run-InteractiveInstaller {
    & (Join-Path $ScriptDir "install.ps1") -Quick:$Quick -PythonPath $PythonPath
}

switch ($Target) {
    "local" { Deploy-Local }
    "claude" { Deploy-Claude }
    "codex" { Deploy-Codex }
    "kilo" { Deploy-Kilo }
    "interactive" { Run-InteractiveInstaller }
    "all" {
        Deploy-Local
        Deploy-Claude
        Deploy-Codex
    }
}

Write-Host "`nDone." -ForegroundColor Green
