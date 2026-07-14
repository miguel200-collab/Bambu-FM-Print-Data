@echo off
:: launcher.bat — starts the MakerLAB daemon with no visible console window.
:: This is the file that Task Scheduler calls at login.

cd /d "%~dp0"
start "" pythonw main.py
