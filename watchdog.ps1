<#
.SYNOPSIS
    Health task: keeps the controller running and the files intact.

.DESCRIPTION
    Runs every 10 minutes from the CloudSyncHealth scheduled task.

      1. destroy.flag present  -> remove both tasks and exit.
      2. restore any missing/tampered payload file from the shadow copy.
      3. roll back a controller update that failed to get going.
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
                Copy-Item -LiteralPath $bak -Destination $cur -Force
                $restored++
                Say "restored $name from shadow copy"
            } else {
                Say "missing $name and no shadow copy"
            }
        }
    }
}

# ---- 3. update rollback ---------------------------------------------------
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

# ---- 4. is the controller running? ---------------------------------------
$alive = $false
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    ForEach-Object {
        if ($_.CommandLine -and $_.CommandLine -like '*controller.py*' -and $_.CommandLine -like "*$([IO.Path]::GetFileName($dir))*" ) { $alive = $true }
        elseif ($_.CommandLine -and $_.CommandLine -like "*$controller*") { $alive = $true }
    }

if (-not $alive) {
    Say "controller not running - starting"
    if ((Test-Path $pythonw) -and (Test-Path $controller)) {
        Start-Process -FilePath $pythonw -ArgumentList @("`"$controller`"") -WorkingDirectory $dir -WindowStyle Hidden
        if ($info.tasks) { schtasks.exe /Run /TN $info.tasks[0] 2>$null | Out-Null }
        Start-Sleep -Seconds 3
    } else {
        Say "cannot start: pythonw=$pythonw controller=$controller"
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
                schtasks.exe /Create /TN $t /TR "`"$pythonw`" `"$controller`"" /SC ONLOGON /RL LIMITED /F /RU $env:USERNAME | Out-Null
                Say "recreated main task"
            }
        }
        $i++
    }
}

if ($restored -gt 0) { Say "restored $restored file(s)" }
