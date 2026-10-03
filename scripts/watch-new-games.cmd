@echo off
REM ===========================================================================
REM NovaLoc - thin ASCII launcher for the resident watcher PowerShell script.
REM
REM WHY THIS FILE IS ASCII-ONLY
REM   cmd.exe parses .cmd files using the console OEM codepage (GBK on this
REM   machine), NOT UTF-8. A UTF-8 .cmd containing Chinese comments gets
REM   decoded into garbage and cmd then tries to EXECUTE that garbage, failing
REM   with "is not recognized as an internal or external command" on every
REM   line. All Chinese documentation lives in watch-new-games.ps1 and
REM   watch-new-games.md; this launcher stays ASCII so it always parses.
REM
REM WHY IT DELEGATES
REM   The real logic is in watch-new-games.ps1, which PowerShell reads as
REM   Unicode -- so it can hold Chinese comments AND emit correctly encoded
REM   timestamps. See that file's .NOTES for the full story of both traps.
REM
REM USAGE
REM   watch-new-games.cmd                 (library defaults to the real one)
REM   set NOVALOC_LIB=D:\some\library
REM   set NOVALOC_DATA_ROOT=D:\some\data
REM   watch-new-games.cmd
REM ===========================================================================
setlocal

set "PS1=%~dp0watch-new-games.ps1"
if not exist "%PS1%" (
  echo FATAL: missing "%PS1%"
  exit /b 1
)

set "ARGS=-NoProfile -ExecutionPolicy Bypass -File "%PS1%""
if defined NOVALOC_LIB set "ARGS=%ARGS% -Library "%NOVALOC_LIB%""

powershell.exe %ARGS%
exit /b %ERRORLEVEL%
