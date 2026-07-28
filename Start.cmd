@echo off
REM ===================================================================
REM  Start.cmd -- double-click this to launch the G&A Classifier UI.
REM
REM  Windows opens .ps1 files in an editor on double-click instead of
REM  running them, so this wrapper hands off to launch_ui.ps1, which
REM  finds Python, installs dependencies on first run, starts the local
REM  server, and opens your browser to the app.
REM
REM  This window IS the server. To stop it, close this window or press
REM  Ctrl+C. If you just walk away, the server also stops on its own
REM  after ~15 minutes of no activity and clears your uploaded data.
REM ===================================================================

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0System\launch_ui.ps1"

echo.
echo The G^&A Classifier server has stopped. You can close this window.
pause >nul
