@echo off
REM Makes Hyper-Wispr start automatically when you log in to Windows.
set "TARGET=%~dp0run.bat"
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Hyper-Wispr.lnk"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LINK%'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory='%~dp0..'; $s.WindowStyle=7; $s.Save()"
echo Hyper-Wispr will now start when you log in.
pause
