@echo off
REM Same as run.bat but keeps a window open showing what the app is doing. Useful for troubleshooting.
cd /d "%~dp0\.."
".venv\Scripts\python.exe" -m hyper_wispr
pause
