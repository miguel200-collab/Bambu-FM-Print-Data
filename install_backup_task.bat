@echo off
setlocal

echo.
echo  MakerLAB Data Engine -- Nightly Backup Setup
echo  =============================================
echo.
echo  Before running this, set "backup.dest" in config.json to a folder that
echo  OneDrive / Google Drive / Dropbox syncs (see config.json.template).
echo.

set TASK_NAME=MakerLAB Backup
set RUNNER=%~dp0run_backup.bat

echo  Registering Task Scheduler job: "%TASK_NAME%" (daily at 03:00)
echo  Runner: %RUNNER%
echo.

schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "cmd /c \"%RUNNER%\"" ^
  /sc DAILY ^
  /st 03:00 ^
  /f

if errorlevel 1 (
    echo.
    echo  ERROR: Could not create the Task Scheduler job.
    echo  Try running this script as Administrator ^(right-click -^> Run as administrator^).
    pause
    exit /b 1
)

echo.
echo  Success! The dataset will be backed up every night at 03:00.
echo.
echo  Useful commands:
echo    Run now:      schtasks /run /tn "%TASK_NAME%"
echo    Check result: type backup.log
echo    Remove task:  schtasks /delete /tn "%TASK_NAME%" /f
echo.
pause
endlocal
