@echo off
REM ===========================================================================
REM NovaLoc - install / remove / status for the watcher logon autostart.
REM
REM USAGE
REM   install-autostart.cmd            install (creates the Startup shortcut)
REM   install-autostart.cmd remove     remove it
REM   install-autostart.cmd status     show whether it is installed
REM
REM WHY THE STARTUP FOLDER AND NOT A SCHEDULED TASK
REM   Register-ScheduledTask returns "Access is denied" on this machine
REM   (it needs elevation), and approval prompts are disabled in this
REM   session, so no elevated route is available. The Startup folder needs
REM   no elevation and matches the actual requirement: the watcher must be
REM   running while the user is logged in. The tradeoff -- it does not run
REM   before logon -- does not matter for a desktop translation tool.
REM
REM WHY THE SHORTCUT IS MADE VIA "powershell -EncodedCommand"
REM   The repo path contains Chinese characters. Passing that path through
REM   nested cmd -> powershell -> WScript.Shell quoting is where this kind of
REM   script normally breaks. -EncodedCommand takes a base64 UTF-16LE blob,
REM   so there is no quoting or codepage involved at all: one arg, no quotes.
REM   The tricky part was getting the base64 in here: `for /f` over
REM   `powershell -Command ...` is subject to the OEM codepage, so instead
REM   this file runs `powershell -File install-autostart.ps1` and lets
REM   PowerShell do all the work. See that file for the details.
REM
REM WHY THIS FILE IS ASCII-ONLY
REM   cmd.exe parses .cmd using the OEM codepage (GBK here), not UTF-8.
REM ===========================================================================
setlocal

set "PS1=%~dp0install-autostart.ps1"
if not exist "%PS1%" (
  echo FATAL: missing "%PS1%" 1>&2
  exit /b 1
)

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=install"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Action "%ACTION%"
exit /b %ERRORLEVEL%
