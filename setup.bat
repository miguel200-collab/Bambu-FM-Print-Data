@echo off
setlocal

echo.
echo  MakerLAB Data Engine -- Dependency Setup
echo  ==========================================
echo.

:: Verify Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python was not found on PATH.
    echo.
    echo  Install Python 3.11 with one of these methods:
    echo    A^) winget install Python.Python.3.11
    echo    B^) Download from https://www.python.org/downloads/
    echo         ^(check "Add Python to PATH" during install^)
    echo.
    pause
    exit /b 1
)

:: Show the version being used
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  Using: %%v
echo.

:: Install / upgrade dependencies
echo  Installing dependencies from requirements.txt ...
echo.
pip install --upgrade -r "%~dp0requirements.txt"

if errorlevel 1 (
    echo.
    echo  ERROR: pip install failed. See output above for details.
    echo  Try running this script as Administrator if pip cannot write files.
    pause
    exit /b 1
)

echo.
echo  All dependencies installed successfully.
echo  Next step: edit config.json with your printer details, then run launcher.bat.
echo.
pause
endlocal
