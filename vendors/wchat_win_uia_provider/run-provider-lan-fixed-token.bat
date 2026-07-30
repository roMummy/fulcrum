@echo off
setlocal
cd /d "%~dp0"
set WECHAT_UIA_PROVIDER_TOKEN=uia-212-test
py -3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8070 --reload
