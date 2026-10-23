@echo off
REM Hyper-Wispr installer. Double-click to run. Needs internet once, to download
REM Python packages and the speech model. After that everything runs offline.
setlocal
cd /d "%~dp0\.."

where uv >nul 2>nul
if errorlevel 1 (
    echo Installing uv, a small Python installer...
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

echo Creating the app environment...
uv venv --python 3.12 .venv || goto :error

where nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo No NVIDIA graphics card found: using the CPU version.
    uv pip install --python .venv -e . || goto :error
) else (
    echo NVIDIA graphics card found: installing GPU support.
    uv pip install --python .venv -e ".[gpu]" || goto :error
)

echo Downloading the speech model (one time, can take a few minutes)...
.venv\Scripts\python.exe -c "from hyper_wispr import config; from hyper_wispr.speech import Transcriber; print('Speech model ready:', Transcriber(config.load()).description)" || goto :error

where ollama >nul 2>nul
if errorlevel 1 (
    echo.
    echo Ollama is not installed, so the AI cleanup step will be skipped.
    echo To turn it on later, install Ollama from https://ollama.com and run this installer again.
) else (
    echo Downloading the local AI cleanup model...
    ollama pull qwen2.5:3b
)

echo.
echo Done. Double-click scripts\run.bat to start Hyper-Wispr.
pause
exit /b 0

:error
echo.
echo Something went wrong. Please copy the messages above and send them over.
pause
exit /b 1
