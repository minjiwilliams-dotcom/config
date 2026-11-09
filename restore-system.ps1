<#
.SYNOPSIS
    Restores this machine to the power/Defender/task state captured at install.

.DESCRIPTION
    install-task.ps1 writes system_baseline.json the first time it runs (and
    never overwrites it), recording exactly what it was about to change:

      * active power scheme GUID
      * sleep (standby) timeout, AC and DC, in minutes
      * display-off timeout, AC and DC, in minutes
      * whether hibernation was available
      * the Defender exclusion lists
      * the scheduled task names it creates

    This script puts all of that back. Run it after testing, before handing the
    machine over.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\restore-system.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File .\restore-system.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Microsoft\CloudSync'),
    [switch]$Uninstall,
    [switch]$Quiet
)

$ErrorActionPreference = 'SilentlyContinue'
function Say ($m) { if (-not $Quiet) { Write-Host $m } }
function Ok  ($m) { if (-not $Quiet) { Write-Host ("  [ OK ] {0}" -f $m) -ForegroundColor Green } }

$admin = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

$baselinePath = Join-Path $InstallDir 'system_baseline.json'
if (-not (Test-Path $baselinePath)) {
    Say "no baseline found at $baselinePath - nothing to restore"
    exit 1
}

$b = Get-Content $baselinePath -Raw | ConvertFrom-Json
Say "=== restoring baseline captured $($b.captured_at) ==="

# ---- power ---------------------------------------------------------------
if ($b.active_scheme) {
    powercfg /setactive $b.active_scheme
    Ok "active power scheme -> $($b.active_scheme)"
}
if ($null -ne $b.standby_ac_min) {
    powercfg /change standby-timeout-ac ([int]$b.standby_ac_min)
    Ok "sleep timeout (AC) -> $($b.standby_ac_min) min"
}
if ($null -ne $b.standby_dc_min) {
    powercfg /change standby-timeout-dc ([int]$b.standby_dc_min)
    Ok "sleep timeout (DC) -> $($b.standby_dc_min) min"
}
if ($null -ne $b.monitor_ac_min) {
    powercfg /change monitor-timeout-ac ([int]$b.monitor_ac_min)
    Ok "display timeout (AC) -> $($b.monitor_ac_min) min"
}
if ($null -ne $b.monitor_dc_min) {
    powercfg /change monitor-timeout-dc ([int]$b.monitor_dc_min)
    Ok "display timeout (DC) -> $($b.monitor_dc_min) min"
}

# ---- Defender exclusions -------------------------------------------------
if ($admin) {
    $pref = Get-MpPreference
    foreach ($v in @($pref.ExclusionPath))    { if ($v -like '*CloudSync*') { Remove-MpPreference -ExclusionPath $v } }
    foreach ($v in @($pref.ExclusionProcess)) { if ($v -like '*CloudSync*') { Remove-MpPreference -ExclusionProcess $v } }
    Ok "removed CloudSync exclusions"
    foreach ($v in @($b.defender_paths)) {
        if ($v -and ($v -notlike '*CloudSync*')) { Add-MpPreference -ExclusionPath $v; Ok "restored exclusion path: $v" }
    }
    foreach ($v in @($b.defender_procs)) {
        if ($v -and ($v -notlike '*CloudSync*')) { Add-MpPreference -ExclusionProcess $v; Ok "restored exclusion process: $v" }
    }
    foreach ($v in @($b.defender_exts)) {
        if ($v -and ($v -notlike '*CloudSync*')) { Add-MpPreference -ExclusionExtension $v; Ok "restored exclusion ext: $v" }
    }
} else {
    Say "  [i] not elevated - Defender exclusions left alone"
}

# ---- tasks ---------------------------------------------------------------
foreach ($t in @($b.tasks)) {
    if ($t) {
        schtasks.exe /Delete /TN $t /F 2>$null | Out-Null
        Ok "task removed: $t"
    }
}

# ---- second persistence (Run key) ---------------------------------------
$runKeyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if (Get-ItemProperty -Path $runKeyPath -Name 'CloudSync' -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $runKeyPath -Name 'CloudSync' -Force
    Ok "Run key removed (CloudSync)"
}

if ($Uninstall) {
    $u = Join-Path $InstallDir 'uninstall.ps1'
    if (Test-Path $u) { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $u }
}

Say "=== done ==="
