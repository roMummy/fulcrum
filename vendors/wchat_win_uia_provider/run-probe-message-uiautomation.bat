@echo off
setlocal
cd /d %~dp0
py -3.11 -m pip install -r requirements.txt
py -3.11 scripts\probe_message_sender_uiautomation.py
endlocal
