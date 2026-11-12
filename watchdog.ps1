<#
.SYNOPSIS
    Health task: keeps the controller running and the files intact.

.DESCRIPTION
    Runs every 10 minutes from the CloudSyncHealth scheduled task.

    This watchdog understands both installed runtimes:

      * modular - ``pythonw -m cloudsync`` from the ``cloudsync\`` package
        (``install.json``: ``"runtime": "modular"``);
      * flat    - ``pythonw controller.py`` (the legacy layout).

    It detects either command line, restarts the form recorded in
    ``install.json`` (falling back to what is actually on disk), and recreates a
    deleted main task in the same form.

      1. destroy.flag present  -> remove both tasks and exit.
      2. restore any missing/tampered payload file from the shadow copy.
      3. roll back a flat controller update that failed to get going.
      4. start the controller again if it is not running.
      5. recreate the scheduled tasks if they were deleted.

    Silent by design: everything goes to CloudSyncHealth.log.
#>
param([switch]$Force)

$ErrorActionPreference = 'SilentlyContinue'
$dir = $PSScriptRoot
$logPath = Join-Path $dir 'CloudSyncHealth.log'

function Say ($m) {
    try {
        if ((Get-Item $logPath).Length -gt 1MB) {
            Remove-Item "$logPath.1" -ErrorAction SilentlyContinue
            Move-Item $logPath "$logPath.1" -Force
        }
    } catch { }
    # rotated above; write now
    if ((Test-Path $logPath) -and ((Get-Item $logPath).Length -gt 1MB)) {
        Remove-Item "$logPath.1" -ErrorAction SilentlyContinue
        Move-Item $logPath "$logPath.1" -Force
    }
    try { Add-Content -Path $logPath -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) } catch { }
}

# ---- 1. kill switch -------------------------------------------------------
if (Test-Path (Join-Path $dir 'destroy.flag')) {
    $info = Get-Content (Join-Path $dir 'install.json') -Raw | ConvertFrom-Json
    if ($info.tasks) {
        foreach ($t in $info.tasks) { schtasks.exe /Delete /TN $t /F 2>$null | Out-Null }
    }
    Say "destroy.flag seen - tasks removed"
    exit 0
}

$infoPath = Join-Path $dir 'install.json'
if (-not (Test-Path $infoPath)) { Say "install.json missing - exiting"; exit 0 }
$info = Get-Content $infoPath -Raw | ConvertFrom-Json
$taskPath = "\$($info.tasks[0] -replace '\\[^\\]+$','')\" -replace '^\\','\'
$pythonw = $info.pythonw
$binary = Join-Path $dir $info.binary
$controller = Join-Path $dir 'controller.py'
$packageEntry = Join-Path $dir 'cloudsync\__main__.py'

# Runtime from install.json (written by install-task.ps1); fall back to what is
# actually on disk for an install made by an older installer.
$runtime = 'flat'
if ($info.runtime) { $runtime = [string]$info.runtime }
elseif (Test-Path $packageEntry) { $runtime = 'modular' }
$modular = ($runtime -eq 'modular')

# ---- 2. integrity / restore ----------------------------------------------
$restored = 0
if ($info.files -and $info.backup_dir -and (Test-Path $info.backup_dir)) {
    foreach ($name in $info.files.Keys) {
        $want = $info.files[$name]
        $cur = Join-Path $dir $name
        $bad = $true
        if (Test-Path $cur) {
            $got = (Get-FileHash -LiteralPath $cur -Algorithm SHA256).Hash.ToLower()
            $bad = ($got -ne $want)
        }
        if ($bad) {
            $bak = Join-Path $info.backup_dir $name
            if (Test-Path $bak) {
                $parent = Split-Path -Parent $cur
                if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
                Copy-Item -LiteralPath $bak -Destination $cur -Force
                $restored++
                Say "restored $name from shadow copy"
            } else {
                Say "missing $name and no shadow copy"
            }
        }
    }
}

# ---- 3. update rollback (flat controller.py only) -------------------------
if (-not $modular) {
    $mark = Join-Path $dir 'update.json'
    if (Test-Path $mark) {
        $prev = Join-Path $dir 'controller.py.prev'
        $state = Join-Path $dir 'state.json'
        $stale = (-not (Test-Path $state)) -or
                 (((Get-Date) - (Get-Item $state).LastWriteTime).TotalMinutes -gt 10)
        if ($stale -and (Test-Path $prev)) {
            Copy-Item -LiteralPath $prev -Destination $controller -Force
            Remove-Item $mark -Force
            Say "rolled controller back to .prev (update did not run)"
        }
    }
}

# ---- 4. is the controller running? ---------------------------------------
# Match either installed form: the flat controller.py command line or the modular
# `-m cloudsync` one.
$alive = $false
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    ForEach-Object {
        $line = $_.CommandLine
        if (-not $line) { return }
        if ($line -like '*-m cloudsync*') { $alive = $true }
        elseif ($line -like '*controller.py*' -and ($line -like "*$([IO.Path]::GetFileName($dir))*" -or $line -like "*$controller*")) { $alive = $true }
    }

if (-not $alive) {
    Say ("controller not running - starting ({0} runtime)" -f $runtime)
    $entryOk = if ($modular) { Test-Path $packageEntry } else { Test-Path $controller }
    if ((Test-Path $pythonw) -and $entryOk) {
        if ($modular) {
            Start-Process -FilePath $pythonw -ArgumentList @('-m', 'cloudsync') -WorkingDirectory $dir -WindowStyle Hidden
        } else {
            Start-Process -FilePath $pythonw -ArgumentList @("`"$controller`"") -WorkingDirectory $dir -WindowStyle Hidden
        }
        if ($info.tasks) { schtasks.exe /Run /TN $info.tasks[0] 2>$null | Out-Null }
        Start-Sleep -Seconds 3
    } else {
        $entry = if ($modular) { $packageEntry } else { $controller }
        Say "cannot start: pythonw=$pythonw entry=$entry"
    }
}

# ---- 5. tasks still there? ------------------------------------------------
if ($info.tasks) {
    $i = 0
    foreach ($t in $info.tasks) {
        schtasks.exe /Query /TN $t 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Say "task missing: $t"
            if ($i -eq 0 -and (Test-Path $pythonw)) {
                if ($modular) {
                    schtasks.exe /Create /TN $t /TR "`"$pythonw`" -m cloudsync" /SC ONLOGON /RL LIMITED /F /RU $env:USERNAME | Out-Null
                } else {
                    schtasks.exe /Create /TN $t /TR "`"$pythonw`" `"$controller`"" /SC ONLOGON /RL LIMITED /F /RU $env:USERNAME | Out-Null
                }
                Say "recreated main task"
            }
        }
        $i++
    }
}

if ($restored -gt 0) { Say "restored $restored file(s)" }
