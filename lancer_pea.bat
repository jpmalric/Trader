@echo off
cd /d "%~dp0"
python pea_screener.py %*
echo.
pause
