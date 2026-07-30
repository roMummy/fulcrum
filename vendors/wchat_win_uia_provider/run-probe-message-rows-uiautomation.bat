@echo off
setlocal
cd /d %~dp0
py -3.11 -m pip install -r requirements.txt
py -3.11 scripts\probe_message_rows_uiautomation.py > output\uiautomation_message_rows_probe.stdout.log 2>&1
echo.
echo Probe finished. Press any key to exit.
if exist output\uiautomation_message_rows_probe.stdout.log type output\uiautomation_message_rows_probe.stdout.log
pause >nul
endlocal
