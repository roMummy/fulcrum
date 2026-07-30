@echo off
setlocal
cd /d "%~dp0"
python debug_windows.py
echo.
echo Debug window list saved to data\debug_windows.json
pause
