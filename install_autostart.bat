@echo off
setlocal

echo.
echo  MakerLAB Data Engine -- Auto-Start Setup
echo  ==========================================
echo.

set TASK_NAME=MakerLAB Daemon
set LAUNCHER=%~dp0launcher.bat

echo  Registering Task Scheduler job: "%TASK_NAME%"
echo  Launcher path: %LAUNCHER%
echo.

:: /sc ONLOGON  → runs at every user login
:: /rl HIGHEST  → runs with the highest available privilege (needed so the
::                window can appear on top of other apps)
:: /f           → overwrite if the task already exists
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "cmd /c \"%LAUNCHER%\"" ^
  /sc ONLOGON ^
  /rl HIGHEST ^
  /f

if errorlevel 1 (
    echo.
    echo  ERROR: Could not create the Task Scheduler job.
    echo  Try running this script as Administrator ^(right-click → Run as administrator^).
    pause
    exit /b 1
)

echo.
echo  Success! The daemon will now start automatically at login.
echo.
echo  Useful commands:
echo    Start now:    schtasks /run  /tn "%TASK_NAME%"
echo    Stop:         schtasks /end  /tn "%TASK_NAME%"
echo    Remove task:  schtasks /delete /tn "%TASK_NAME%" /f
echo.
pause
endlocal
