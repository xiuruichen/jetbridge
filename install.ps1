# jetbridge installer for Windows.
#
#   irm https://raw.githubusercontent.com/xiuruichen/jetbridge/main/install.ps1 | iex
#
# It installs Git, uv and Google Chrome if they're missing (asking first), downloads jetbridge
# to your home folder, installs its Python packages, adds a `jetbridge` command that works from
# any window, offers to connect Claude Desktop if you have it, and starts `jetbridge start`.
#
# Optional settings, as environment variables:
#   JETBRIDGE_DIR     where to put jetbridge (default: your home folder\jetbridge)
#   JETBRIDGE_REPO    where to download it from
#   JETBRIDGE_BRANCH  which branch to download
#   JETBRIDGE_INSTALL_NO_START=1  install, but don't ask anything or start the questions
#   JETBRIDGE_INSTALL_NO_PATH=1   don't add the `jetbridge` command to your PATH

function Invoke-Native {
    # Windows PowerShell can treat a program's normal progress output as an error, so the exit
    # code decides here.
    param([string]$Program, [string[]]$Arguments, [string]$Failure)
    $ErrorActionPreference = 'Continue'
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Failure (exit code $LASTEXITCODE)" }
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user;$env:Path"
}

function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $places = @("$env:USERPROFILE\.local\bin", "$env:LOCALAPPDATA\Microsoft\WinGet\Packages")
    foreach ($place in $places) {
        if (Test-Path $place) {
            $found = Get-ChildItem $place -Recurse -Filter uv.exe -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($found) { return $found.FullName }
        }
    }
    return $null
}

function Test-Chrome {
    $places = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    foreach ($place in $places) {
        if (Test-Path $place) { return $true }
    }
    return $false
}

function Install-MissingTools {
    $missing = @()
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        $missing += [pscustomobject]@{ Name = 'Git'; Id = 'Git.Git' }
    }
    if (-not (Find-Uv)) {
        $missing += [pscustomobject]@{ Name = 'uv'; Id = 'astral-sh.uv' }
    }
    if (-not (Test-Chrome)) {
        $missing += [pscustomobject]@{ Name = 'Google Chrome'; Id = 'Google.Chrome' }
    }
    if ($missing.Count -eq 0) {
        Write-Host 'Git, uv and Google Chrome are already installed.'
        return
    }

    $names = ($missing | ForEach-Object { $_.Name }) -join ', '
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Please install $names, then run this again. winget isn't available on this PC."
    }
    Write-Host "jetbridge needs: $names"
    $answer = Read-Host 'Install with winget and accept their license terms? [y/N]'
    if ($answer -notmatch '^(y|yes)$') { throw "Nothing was installed. jetbridge needs $names." }

    foreach ($tool in $missing) {
        Write-Host "Installing $($tool.Name)..."
        Invoke-Native winget @(
            'install', '--id', $tool.Id, '-e', '--accept-source-agreements',
            '--accept-package-agreements'
        ) "Couldn't install $($tool.Name)"
    }
    Update-SessionPath
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'Git was installed, but this window cannot see it yet. Open a new PowerShell window and run this again.'
    }
    if (-not (Test-Chrome)) {
        throw 'Google Chrome was not found after installing. Install it from google.com/chrome and run this again.'
    }
}

function Get-Jetbridge {
    param([string]$Target, [string]$Repo, [string]$Branch)
    if (Test-Path $Target) {
        if (-not (Test-Path (Join-Path $Target 'pyproject.toml')) -or
            -not (Test-Path (Join-Path $Target '.git'))) {
            throw "$Target already exists and isn't jetbridge, so I left it alone. Set JETBRIDGE_DIR to use another folder."
        }
        Write-Host "jetbridge is already in $Target. Checking for updates..."
        $ErrorActionPreference = 'Continue'
        & git -C $Target pull --ff-only
        if ($LASTEXITCODE -ne 0) { Write-Host 'Could not update it, so I will use the copy you have.' }
        return
    }
    Write-Host "Downloading jetbridge to $Target..."
    $arguments = @('clone')
    if ($Branch) { $arguments += @('--branch', $Branch) }
    $arguments += @($Repo, $Target)
    Invoke-Native git $arguments "Couldn't download jetbridge"
}

function Set-EnvironmentOutsideSyncedFolder {
    param([string]$Target)
    $full = [IO.Path]::GetFullPath($Target).TrimEnd('\') + '\'
    foreach ($synced in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
        if ($synced -and $full.StartsWith($synced.TrimEnd('\') + '\', 'OrdinalIgnoreCase')) {
            # Python environments don't belong in a synced folder. This matches the README.
            $venv = "$env:LOCALAPPDATA\jetbridge\venv"
            Write-Host "$Target is in OneDrive, so its Python packages will go in $venv instead."
            [Environment]::SetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', $venv, 'User')
            $env:UV_PROJECT_ENVIRONMENT = $venv
            return
        }
    }
}

function Get-EnvironmentFolder {
    param([string]$Target)
    if ($env:UV_PROJECT_ENVIRONMENT) { return [IO.Path]::Combine($Target, $env:UV_PROJECT_ENVIRONMENT) }
    return Join-Path $Target '.venv'
}

function Write-Launcher {
    # A small file that runs jetbridge. Its folder goes on PATH instead of the Python environment's,
    # which would also put that environment's python.exe ahead of the person's own.
    param([string]$Program, [string]$Folder)
    [void](New-Item -ItemType Directory -Force -Path $Folder)
    # cmd.exe reads batch files in the console's code page, so this keeps a non-English username.
    $encoding = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
    [IO.File]::WriteAllText((Join-Path $Folder 'jetbridge.cmd'), "@`"$Program`" -m jetbridge %*`r`n", $encoding)
}

function Add-FolderToPathText {
    param([string]$Current, [string]$Folder)
    $wanted = $Folder.TrimEnd('\')
    foreach ($entry in ($Current -split ';')) {
        if ($entry.TrimEnd('\') -ieq $wanted) { return $Current }
    }
    if (-not $Current.Trim()) { return $Folder }
    return $Current.TrimEnd(';') + ';' + $Folder
}

function Add-FolderToUserPath {
    # Through the registry, because [Environment] would expand entries like %USERPROFILE%\bin
    # and save them that way.
    param([string]$Folder, [string]$KeyName = 'Environment')
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($KeyName, $true)
    try {
        $raw = [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
        $current = [string]$key.GetValue('Path', '', $raw)
        $updated = Add-FolderToPathText $current $Folder
        if ($updated -cne $current) {
            $key.SetValue('Path', $updated, [Microsoft.Win32.RegistryValueKind]::ExpandString)
            # Clearing an unused variable tells Windows to let new windows see the change.
            [Environment]::SetEnvironmentVariable('JETBRIDGE_PATH_CHANGED', $null, 'User')
        }
    } finally {
        $key.Close()
    }
}

function Install-Jetbridge {
    $ErrorActionPreference = 'Stop'
    $target = if ($env:JETBRIDGE_DIR) { $env:JETBRIDGE_DIR } else { Join-Path $env:USERPROFILE 'jetbridge' }
    $repo = if ($env:JETBRIDGE_REPO) { $env:JETBRIDGE_REPO } else { 'https://github.com/xiuruichen/jetbridge.git' }

    Install-MissingTools
    Get-Jetbridge $target $repo $env:JETBRIDGE_BRANCH
    Set-EnvironmentOutsideSyncedFolder $target

    $uv = Find-Uv
    if (-not $uv) {
        throw 'uv was installed, but this window cannot see it yet. Open a new PowerShell window and run this again.'
    }
    Write-Host 'Installing the Python packages jetbridge needs...'
    # An older connection to Claude Desktop can keep a program open that this has to replace.
    Invoke-Native $uv @('sync', '--project', $target) "Couldn't install the Python packages. If Claude Desktop is open, quit it and run this again"

    $program = Join-Path (Get-EnvironmentFolder $target) 'Scripts\jetbridge.exe'
    $python = Join-Path (Get-EnvironmentFolder $target) 'Scripts\python.exe'
    $launcher = Join-Path $env:LOCALAPPDATA 'jetbridge\bin'
    # Through python, so a running command doesn't lock a program that an update has to replace.
    Write-Launcher $python $launcher

    Write-Host ''
    Write-Host "jetbridge is installed in $target."
    if ($env:JETBRIDGE_INSTALL_NO_PATH -eq '1') {
        Write-Host "To add flights later, run: & `"$program`" start"
    } else {
        Add-FolderToUserPath $launcher
        $env:Path = Add-FolderToPathText $env:Path $launcher
        Write-Host 'To add flights later, run this in any PowerShell window: jetbridge start'
    }
    Write-Host ''
    if ($env:JETBRIDGE_INSTALL_NO_START -eq '1') { return }

    # Neither Claude Desktop nor stopping part way through the questions is an installer failure.
    $ErrorActionPreference = 'Continue'
    & $program claude connect --ask
    Write-Host ''
    & $program start
}

$jetbridgeInstallFailed = $false
try {
    Install-Jetbridge
} catch {
    $jetbridgeInstallFailed = $true
    Write-Host ''
    Write-Host "jetbridge wasn't fully installed: $($_.Exception.Message)" -ForegroundColor Red
}
# Piped into iex there's no script path, and `exit` would close the person's window.
if ($jetbridgeInstallFailed -and $PSCommandPath) { exit 1 }
