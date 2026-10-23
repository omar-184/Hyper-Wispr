@echo off
REM Starts Hyper-Wispr in the system tray (no console window).
cd /d "%~dp0\.."
start "" ".venv\Scripts\pythonw.exe" -m hyper_wispr
