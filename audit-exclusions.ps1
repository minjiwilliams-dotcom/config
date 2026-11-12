<#
.SYNOPSIS
    Report whether the running miner binary is actually covered by a Defender
    exclusion - the failure mode where an old install excludes xmrig.exe while
    the box runs svchost.exe.

.DESCRIPTION
    Read-only. For each detected install it prints the configured binary, the
    executables present, the live state and every Defender exclusion that could
    apply, then gives a verdict:

        COVERED-PROCESS    the resolved binary has a process exclusion
        COVERED-DIRECTORY  an excluded folder contains the binary
        NOT-COVERED        the binary is excluded by nothing (legacy defect)
        NO-INSTALL         nothing installed on this box
        UNKNOWN            Defender preferences could not be read

    Exit codes: 0 covered or no install, 1 NOT-COVERED, 2 Defender unreadable.

.PARAMETER InstallDir
    Check one install directory instead of the two default locations.

.EXAMPLE
    powershell -NoProfile -File .\audit-exclusions.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallDir
)

$ErrorActionPreference = 'SilentlyContinue'

function Say  ($m) { Write-Host $m }
function Good ($m) { Write-Host ("  [ OK ] {0}" -f $m) -ForegroundColor Green }
function Warn ($m) { Write-Host ("  [WARN] {0}" -f $m) -ForegroundColor Yellow }
function Bad  ($m) { Write-Host ("  [FAIL] {0}" -f $m) -ForegroundColor Red }

$candidates = @()
if ($InstallDir) {
    $candidates += $InstallDir
} else {
    $candidates += (Join-Path $env:ProgramData 'Microsoft\CloudSync')
    $candidates += (Join-Path $env:LOCALAPPDATA 'Microsoft\CloudSync')
}

$defenderOk = $true
$paths = @()
$procs = @()
try {
    $mp = Get-MpPreference -ErrorAction Stop
    $paths = @($mp.ExclusionPath | Where-Object { $_ })
    $procs = @($mp.ExclusionProcess | Where-Object { $_ })
} catch {
    $defenderOk = $false
}

Say "=== CloudSync binary / exclusion audit ==="
Say ("computer : {0}" -f $env:COMPUTERNAME)
Say ("when     : {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
if ($defenderOk) {
    Say ("defender : {0} path exclusion(s), {1} process exclusion(s)" -f $paths.Count, $procs.Count)
} else {
    Warn "defender : Get-MpPreference unavailable - cannot judge coverage"
}

$found = $false
$anyMissing = $false

foreach ($dir in $candidates) {
    $installJson = Join-Path $dir 'install.json'
    if (-not (Test-Path -LiteralPath $installJson)) { continue }
    $found = $true

    Say ""
    Say ("--- install: {0}" -f $dir)
    $info = $null
    try { $info = Get-Content -LiteralPath $installJson -Raw | ConvertFrom-Json } catch { }
    $configured = if ($info.binary) { $info.binary } else { '(not recorded)' }
    $runtime = if ($info.runtime) { $info.runtime } else { '(not recorded)' }
    Say ("  configured binary : {0}" -f $configured)
    Say ("  runtime           : {0}" -f $runtime)

    $exes = @(Get-ChildItem -LiteralPath $dir -Filter *.exe -File | Select-Object -ExpandProperty Name)
    Say ("  executables       : {0}" -f (($exes -join ', ')))

    # what the package would actually run: configured name, then kit fallbacks
    $resolved = $null
    foreach ($name in @($configured, 'CloudSync.exe', 'svchost.exe', 'xmrig.exe', 'WinSync.exe')) {
        if (-not $name -or $name -like '(*') { continue }
        if (Test-Path -LiteralPath (Join-Path $dir $name)) { $resolved = $name; break }
    }
    if (-not $resolved) {
        $first = @($exes | Select-Object -First 1)
        if ($first.Count -gt 0) { $resolved = $first[0] }
    }
    Say ("  resolved binary   : {0}" -f ($(if ($resolved) { $resolved } else { '(none)' })))

    $stateFile = Join-Path $dir 'state.json'
    if (Test-Path -LiteralPath $stateFile) {
        try {
            $s = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
            Say ("  state             : running={0} pid={1} profile={2} cap={3}%" -f $s.running, $s.pid, $s.profile, $s.cpu_cap_pct)
        } catch { Warn "state.json unreadable" }
    } else {
        Warn "state.json missing (no tick yet?)"
    }

    if (-not $defenderOk) { Warn "coverage unknown"; continue }
    if (-not $resolved) { Warn "no executable to judge"; continue }

    $full = [IO.Path]::GetFullPath((Join-Path $dir $resolved))
    $matchedProc = @($procs | Where-Object {
        $p = [IO.Path]::GetFullPath((Join-Path $dir (Split-Path $_ -Leaf)))
        $p -ieq $full
    })
    $matchedDir = @($paths | Where-Object {
        $d = $_.TrimEnd('\')
        $d -and ($full -like ($d + '\*'))
    })

    if ($matchedProc.Count -gt 0) {
        Good ("COVERED-PROCESS - {0} excluded as a process" -f $resolved)
    } elseif ($matchedDir.Count -gt 0) {
        Good ("COVERED-DIRECTORY - covered by excluded path {0}" -f $matchedDir[0])
    } else {
        $anyMissing = $true
        Bad ("NOT-COVERED - {0} is excluded by nothing" -f $resolved)
        $others = @($procs | Where-Object { $_ -match 'xmrig|svchost|CloudSync|WinSync' })
        if ($others.Count -gt 0) {
            Say ("    exclusions exist for other names: {0}" -f ($others -join '; '))
            Say  "    (this is the legacy pattern - the excluded name is not the running name)"
        }
    }
}

if (-not $found) {
    Say ""
    Say "  NO-INSTALL - no install.json in:"
    $candidates | ForEach-Object { Say ("    {0}" -f $_) }
    exit 0
}

Say ""
if (-not $defenderOk) { Say "VERDICT: UNKNOWN"; exit 2 }
if ($anyMissing) { Say "VERDICT: NOT-COVERED (at least one install)"; exit 1 }
Say "VERDICT: COVERED"
exit 0
