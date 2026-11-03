@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Dividend Dashboard V4 - ALWAYS OPEN
:RUN
cls
echo ==================================================================
echo  DIVIDEND DASHBOARD V4 - ALWAYS OPEN LAUNCHER
echo  Version marker: V4-20261103-NO-PAUSE
 echo ==================================================================
echo  IMPORTANT: No key can close this launcher during an error.
echo  Browser: http://127.0.0.1:8765
echo.
call "%~dp0scripts\start_core.cmd"
set "LAUNCH_RESULT=%ERRORLEVEL%"
echo.
echo ==================================================================
echo  Web process ended or could not start. Exit code: %LAUNCH_RESULT%
echo  Please read the error ABOVE and files: INSTALL_LOG.txt / RUN_LOG.txt
echo  To see diagnostics, select option D in the menu below.
echo ==================================================================
:MENU
echo.
echo  [R] Retry starting     [D] Generate diagnosis
 echo  [O] Open browser       [Q] Quit this window
set "ACTION="
set /p "ACTION=Enter choice and press ENTER: "
if /I "%ACTION%"=="R" goto RUN
if /I "%ACTION%"=="D" (
  call "%~dp0scripts\diagnose_core.cmd"
  goto MENU
)
if /I "%ACTION%"=="O" (
  start "" "http://127.0.0.1:8765"
  goto MENU
)
if /I "%ACTION%"=="Q" exit /b 0
echo Invalid choice. The window stays open.
goto MENU