@echo off
setlocal
cd /d %~dp0
py -3.11 -m pip install -r requirements.txt
py -3.11 scripts\probe_chat_history_uiautomation.py
endlocal
