# Windows 10/11, PowerShell 5.1 or newer. No administrator privileges required.
[CmdletBinding()]
param(
    [switch]$RegisterCommandOnly,
    [switch]$NoUserPath
)

function Add-OrpheusPathEntry {
    param([AllowNull()][string]$ExistingPath, [string]$InstallRoot)
    $normalized = $InstallRoot.TrimEnd('\', '/')
    $remaining = @()
    if ($ExistingPath) {
        foreach ($entry in ($ExistingPath -split ';')) {
            $expanded = [Environment]::ExpandEnvironmentVariables($entry.Trim('"')).TrimEnd('\', '/')
            if (-not [string]::Equals($expanded, $normalized, [StringComparison]::OrdinalIgnoreCase)) {
                $remaining += $entry
            }
        }
    }
    return (@($InstallRoot) + $remaining) -join ';'
}

function Invoke-OrpheusChecked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable failed (exit $LASTEXITCODE). Installation stopped."
    }
}

function Send-OrpheusEnvironmentChange {
    # Let Explorer/new terminals notice the updated user PATH. Existing shells
    # still need to restart; their inherited process environment is unchanged.
    if (-not ('OrpheusEnvironmentBroadcast' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class OrpheusEnvironmentBroadcast {
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint message,
        UIntPtr wparam, string lparam, uint flags, uint timeout, out UIntPtr result);
}
'@
    }
    $result = [UIntPtr]::Zero
    [void][OrpheusEnvironmentBroadcast]::SendMessageTimeout(
        [IntPtr]0xffff, 0x1a, [UIntPtr]::Zero, 'Environment', 2, 1000, [ref]$result)
}

function Install-Orpheus {
    param([string]$InstallRoot, [switch]$OnlyCommand, [switch]$TransientPath)
    $ErrorActionPreference = 'Stop'
    $InstallRoot = (Resolve-Path -LiteralPath $InstallRoot).Path
    if ($InstallRoot.Contains(';')) { throw 'The installation path cannot contain a semicolon.' }
    $entryPoint = Join-Path $InstallRoot 'orpheus.py'
    $launcher = Join-Path $InstallRoot 'orpheus.cmd'
    $python = Join-Path $InstallRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $entryPoint) -or -not (Test-Path -LiteralPath $launcher)) {
        throw 'Run this installer from a complete OrpheusDL checkout.'
    }
    # Second command: abq (artist_best_quality.py). Soft check so an older
    # checkout without it still installs the orpheus command normally.
    $abqEntry = Join-Path $InstallRoot 'artist_best_quality.py'
    $abqLauncher = Join-Path $InstallRoot 'abq.cmd'
    $abqAvailable = (Test-Path -LiteralPath $abqEntry) -and (Test-Path -LiteralPath $abqLauncher)
    # Inspect current state before making any PATH/config changes.
    $oldUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $oldProcessPath = $env:Path
    $oldCommand = Get-Command orpheus -ErrorAction SilentlyContinue
    if ($oldCommand) { Write-Host "Existing orpheus command: $($oldCommand.Source)" }
    $steps = [System.Collections.Generic.List[string]]::new()
    Push-Location -LiteralPath $InstallRoot
    try {
        if (-not $OnlyCommand) {
            if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git first, then open a new PowerShell window.' }
            if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'Install FFmpeg and add its bin folder to PATH, then reopen PowerShell.' }
            if (-not (Test-Path -LiteralPath $python)) {
                if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Install Python 3.13 (64-bit), including its py launcher.' }
                Invoke-OrpheusChecked 'py' @('-3.13', '-m', 'venv', '.venv')
            }
            Invoke-OrpheusChecked $python @('-c', 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required; 3.13 recommended"')
            $steps.Add('Python environment verified')
            $modules = @(
                @{ Name='qobuz'; Repo='https://github.com/np3ir/orpheusdl-qobuz.git'; Branch='feat/rate-limit-flac-only' },
                @{ Name='deezer'; Repo='https://github.com/np3ir/OrpheusDL-deezer.git'; Branch='feat/rate-limit-flac-only' },
                @{ Name='tidal'; Repo='https://github.com/np3ir/orpheusdl-tidal.git'; Branch='feat/rate-limit-flac-only' },
                @{ Name='spotify'; Repo='https://github.com/np3ir/orpheusdl-spotify.git'; Branch='codex/flac-only' }
            )
            foreach ($module in $modules) {
                $destination = Join-Path 'modules' $module.Name
                if (Test-Path -LiteralPath $destination) {
                    if (-not (Test-Path -LiteralPath (Join-Path $destination 'interface.py'))) {
                        throw "$destination already exists but is incomplete; inspect it before retrying."
                    }
                    Write-Host "Preserving existing module: $($module.Name)"
                } else {
                    Invoke-OrpheusChecked 'git' @('clone', '--recurse-submodules', '-b', $module.Branch, $module.Repo, $destination)
                }
                $steps.Add("Module ready: $($module.Name)")
            }
            Invoke-OrpheusChecked $python @('-m', 'pip', 'install', '-r', 'requirements-core.txt')
            Invoke-OrpheusChecked $python @('-m', 'pip', 'check')
            Invoke-OrpheusChecked $python @('orpheus.py', '--help')
            if ($abqAvailable) {
                Invoke-OrpheusChecked $python @('artist_best_quality.py', '--help')
                $steps.Add('abq (artist_best_quality) checked')
            }
            $steps.Add('Dependencies and CLI checked')
            $settings = Join-Path $InstallRoot 'config\settings.json'
            $newSettings = -not (Test-Path -LiteralPath $settings)
            if ($newSettings) {
                Invoke-OrpheusChecked $python @('orpheus.py', 'settings', 'refresh')
                if (-not (Test-Path -LiteralPath $settings)) { throw 'Settings generation failed. See the output above.' }
                Invoke-OrpheusChecked $python @('scripts/configure_install.py', $settings, '--new')
            } else {
                # Existing credentials, paths and preferences are never reset.
                Invoke-OrpheusChecked $python @('scripts/configure_install.py', $settings)
            }
            $steps.Add('Settings created/validated without copying credentials')
        } elseif (-not (Test-Path -LiteralPath $python)) {
            throw 'No .venv found. Run install.ps1 without -RegisterCommandOnly first.'
        }
        # Never replace the whole user PATH or truncate it with setx.
        if (-not $TransientPath) {
            $newUserPath = Add-OrpheusPathEntry $oldUserPath $InstallRoot
            if ($newUserPath -cne $oldUserPath) {
                [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
                try { Send-OrpheusEnvironmentChange } catch {
                    Write-Warning 'PATH saved. Sign out/in if a restarted terminal cannot see the command.'
                }
            }
        }
        $env:Path = Add-OrpheusPathEntry $oldProcessPath $InstallRoot
        $steps.Add('Command registered in process PATH' + $(if ($TransientPath) { '' } else { ' and user PATH' }))
        $configDir = Join-Path $InstallRoot 'config'
        New-Item -ItemType Directory -Force -Path $configDir | Out-Null
        $report = @{
            utc = [DateTime]::UtcNow.ToString('o'); install_root = $InstallRoot
            mode = $(if ($OnlyCommand) { 'register-command' } else { 'install' })
            user_path_updated = (-not $TransientPath); completed_steps = @($steps.ToArray())
        }
        $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $configDir 'install-report.json') -Encoding UTF8
        Write-Host ''
        if ($TransientPath) {
            Write-Host 'Ready for this process only (-NoUserPath). No persistent PATH change was made.'
        } else {
            Write-Host 'Ready. Open a NEW terminal (restart Windows Terminal if necessary), then run:'
        }
        Write-Host '  orpheus "https://open.qobuz.com/track/441053229"'
        if ($abqAvailable) {
            Write-Host '  abq     "https://tidal.com/artist/10411"        # el artista completo, al mejor FLAC entre servicios'
        }
        Write-Host 'Use a plain URL, not [text](URL). Configure your account in config/settings.json.'
        Write-Host 'Existing terminals may still have their old PATH. No permanent execution-policy change was made.'
    } finally {
        Pop-Location
    }
}

# Dot-sourcing exposes the pure helper for tests without installing anything.
if ($MyInvocation.InvocationName -ne '.') {
    try {
        Install-Orpheus -InstallRoot $PSScriptRoot -OnlyCommand:$RegisterCommandOnly -TransientPath:$NoUserPath
    } catch {
        Write-Error $_
        exit 1
    }
}
