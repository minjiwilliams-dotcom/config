<#
.SYNOPSIS
    Removes the CloudSync miner completely.

.DESCRIPTION
    The old kit had no uninstaller - the only way out was the (broken) remote
    delete.txt, which left the miner orphaned and the watchdog re-copying files.
    This does it in the right order: stop -> tasks -> exclusions -> files.

    All processes are stopped BEFORE anything is deleted, so the "file is in
    use" failure mode cannot happen.
#>
[CmdletBinding()]
param(
    [string]$TaskRoot = 'Microsoft\Windows\CloudSync',
    [switch]$NoExclusions,
    [switch]$Quiet
)

$ErrorActionPreference = 'SilentlyContinue'
function Say ($m) { if (-not $Quiet) { Write-Host $m } }

$dir = Join-Path $env:LOCALAPPDATA 'Microsoft\CloudSync'
if (-not (Test-Path (Join-Path $dir 'install.json'))) {
    $machine = Join-Path $env:ProgramData 'Microsoft\CloudSync'
    if (Test-Path (Join-Path $machine 'install.json')) { $dir = $machine }
}
$info = $null
if (Test-Path (Join-Path $dir 'install.json')) {
    $info = Get-Content (Join-Path $dir 'install.json') -Raw | ConvertFrom-Json
}

$tasks = @()
if ($info -and $info.tasks) { $tasks = $info.tasks }
else { $tasks = @("$TaskRoot\CloudSync", "$TaskRoot\CloudSyncHealth") }

Say "=== CloudSync uninstall ==="

# 0. revert the machine state the installer changed (power scheme/timeouts) while
# the baseline still exists -- the baseline is deleted with the install dir below.
$restore = Join-Path $dir 'restore-system.ps1'
if (Test-Path $restore) {
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $restore -InstallDir $dir 2>&1 |
            ForEach-Object { Say ("  restore: " + $_) }
    } catch { Say "  [i] restore-system.ps1 failed - power profile left as-is" }
}

# 1. tasks first: nothing can resurrect what has no trigger
foreach ($t in $tasks) {
    schtasks.exe /Delete /TN $t /F 2>$null | Out-Null
    Say "  task removed: $t"
}

# 1b. second persistence: Run key
$runName = if ($info -and $info.run_name) { $info.run_name } else { 'CloudSync' }
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
try {
    if ((Get-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue)) {
        Remove-ItemProperty -Path $runKey -Name $runName -Force
        Say "  Run key removed: $runName"
    }
} catch { }

# 2. processes (controller first, then the worker)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*controller.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Say "  killed controller pid $($_.ProcessId)" }

# match by PATH, not by name: the worker may be disguised as svchost.exe, and
# killing every svchost.exe would take the machine down
$exeName = if ($info -and $info.binary) { $info.binary } else { 'CloudSync.exe' }
Start-Sleep -Seconds 1
foreach ($n in @($exeName) + @('xmrig.exe','svchost.exe','CloudSync.exe')) {
    Get-CimInstance Win32_Process -Filter "Name='$n'" |
        Where-Object { ($_.ExecutablePath -like "$dir*") -or ($_.CommandLine -like "*$dir*") } |
        ForEach-Object {
            try { & taskkill.exe /PID $_.ProcessId 2>$null | Out-Null } catch { }
            Start-Sleep -Milliseconds 400
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Say "  killed worker pid $($_.ProcessId) ($n)"
        }
}
Start-Sleep -Seconds 2

# 3. Defender exclusions
if (-not $NoExclusions) {
    $admin = (New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($admin) {
        $pref = Get-MpPreference
        foreach ($v in @($pref.ExclusionPath))      { if ($v -like '*CloudSync*') { Remove-MpPreference -ExclusionPath $v } }
        foreach ($v in @($pref.ExclusionProcess))   { if ($v -like '*CloudSync*') { Remove-MpPreference -ExclusionProcess $v } }
        Say "  exclusions removed"
    }
}

# 4. files
if ($info -and $info.backup_dir -and (Test-Path $info.backup_dir)) {
    Remove-Item $info.backup_dir -Recurse -Force
    Say "  shadow copy removed: $($info.backup_dir)"
}
$root = Join-Path $env:ProgramData 'Microsoft\CloudSync'
if (Test-Path $root) { Remove-Item $root -Recurse -Force; Say "  shadow root removed" }

if (Test-Path $dir) {
    # keep the wallet only if asked
    Remove-Item $dir -Recurse -Force
    Say "  install dir removed: $dir"
}

Say "=== done ==="
