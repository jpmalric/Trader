@echo off
cd /d "%~dp0"
python world_screener.py %*
echo.
pause
