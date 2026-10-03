@echo off
REM ===================================================================
REM  NovaLoc busy-watcher launcher  (ASCII ONLY - do not add Chinese)
REM
REM  cmd.exe parses .cmd files in the OEM codepage (GBK on this box).
REM  UTF-8 Chinese bytes get reinterpreted as GBK, which can splice a
REM  quote or an ampersand into the MIDDLE of a string and break the
REM  whole script with an unreadable parser error.
REM  Measured failure: a Chinese window title in `start "..."` split the
REM  command line. => keep this file pure ASCII; Chinese lives in the .ps1.
REM
REM  Why delegate to PowerShell instead of running python directly:
REM  python.exe is a console app, so calling it from cmd would grab a
REM  console window. Start-Process -WindowStyle Hidden gives no window
REM  and returns immediately (~0 s measured), so ending the parent does
REM  not kill the watcher.
REM ===================================================================
setlocal
set "PS1=%~dp0watch-busy.ps1"
if not exist "%PS1%" (
  echo [ERROR] not found: "%PS1%"
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
exit /b %ERRORLEVEL%
