@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ---- No file dropped: show the numbered picker menu ----
if "%~1"=="" (
  python readaloud.py
  goto end
)

rem ---- One or more files dropped: convert each of them ----
:loop
if "%~1"=="" goto end
python readaloud.py %1
shift
goto loop

:end
echo.
echo Done. You can close this window now.
pause
