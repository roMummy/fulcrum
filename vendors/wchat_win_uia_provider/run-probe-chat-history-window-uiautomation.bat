@echo off
setlocal
cd /d %~dp0
py -3.11 -m pip install -r requirements.txt
py -3.11 scripts\probe_chat_history_window_uiautomation.py > output\uiautomation_chat_history_window_probe.stdout.log 2>&1
echo.
echo Probe finished. Press any key to exit.
if exist output\uiautomation_chat_history_window_probe.stdout.log type output\uiautomation_chat_history_window_probe.stdout.log
pause >nul
endlocal
