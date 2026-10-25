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

echo Installing the built-in AI cleanup engine...
uv pip install --python .venv "llama-cpp-python==0.3.36" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
if errorlevel 1 (
    echo.
    echo The built-in AI engine couldn't be installed, so AI cleanup will use Ollama if it's installed.
    where ollama >nul 2>nul
    if not errorlevel 1 ollama pull qwen2.5:3b
) else (
    echo Downloading the AI cleanup model (one time, about 2 GB^)...
    .venv\Scripts\python.exe -c "from hyper_wispr import config, local_llm; s = config.load()['ai_cleanup']; print('AI model ready:', local_llm.download(s.get('builtin_model') or local_llm.DEFAULT_MODEL))"
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
