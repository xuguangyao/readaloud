@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting the reading workbench...
echo Keep this window open while you are listening. Close it to stop the server.
echo.
python readaloud_web.py
pause
