@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Dividend Dashboard V4 - DIAGNOSIS
 echo ==============================================================
echo  V4-20261103 DIAGNOSIS - THIS WINDOW WILL STAY OPEN
 echo ==============================================================
call "%~dp0scripts\diagnose_core.cmd"
echo.
echo Diagnosis saved to DIAGNOSIS_V4.txt.
:AGAIN
set "A="
set /p "A=Enter R to run again or Q to close (ENTER does nothing): "
if /I "%A%"=="R" (
 call "%~dp0scripts\diagnose_core.cmd"
 goto AGAIN
)
if /I "%A%"=="Q" exit /b 0
goto AGAIN