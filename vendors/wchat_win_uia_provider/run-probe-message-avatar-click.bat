@echo off
setlocal
cd /d %~dp0
set WECHAT_UIA_AVATAR_PROBE_INDEX=%~1
if "%WECHAT_UIA_AVATAR_PROBE_INDEX%"=="" set WECHAT_UIA_AVATAR_PROBE_INDEX=1
py -3.11 -m pip install -r requirements.txt
py -3.11 scripts\probe_message_avatar_click.py > output\uiautomation_message_avatar_probe.stdout.log 2>&1
echo.
echo Probe finished. Press any key to exit.
if exist output\uiautomation_message_avatar_probe.stdout.log type output\uiautomation_message_avatar_probe.stdout.log
pause >nul
endlocal
