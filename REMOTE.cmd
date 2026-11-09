@echo off
REM ============================================================
REM  One-shot remote installer.
REM
REM  Run from an elevated prompt on the target machine, or paste the
REM  whole block into a single PowerShell command line.
REM
REM  Differences from the old version:
REM    * -NoPrompt so it cannot hang on a keypress in a non-interactive
REM      session (the old script ended with ReadKey and hung forever).
REM    * cleans up the temp tree even when the install fails.
REM    * exits with the installer's real exit code.
REM ============================================================

set REPO=minjiwilliams-dotcom/config
set BRANCH=main

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "try {" ^
  "  $zip=Join-Path $env:TEMP 'sysu.zip'; $dir=Join-Path $env:TEMP 'sysu';" ^
  "  [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "  Invoke-WebRequest -UseBasicParsing \"https://github.com/%REPO%/archive/refs/heads/%BRANCH%.zip\" -OutFile $zip;" ^
  "  if (Test-Path $dir) { Remove-Item $dir -Recurse -Force };" ^
  "  Expand-Archive -LiteralPath $zip -DestinationPath $dir -Force;" ^
  "  $src=Join-Path $dir 'config-%BRANCH%';" ^
  "  $rc = (Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',(Join-Path $src 'install-task.ps1'),'-NoPrompt') -Wait -PassThru).ExitCode;" ^
  "  exit $rc" ^
  "} finally {" ^
  "  Remove-Item (Join-Path $env:TEMP 'sysu.zip') -Force -ErrorAction SilentlyContinue;" ^
  "  Remove-Item (Join-Path $env:TEMP 'sysu') -Recurse -Force -ErrorAction SilentlyContinue" ^
  "}"
