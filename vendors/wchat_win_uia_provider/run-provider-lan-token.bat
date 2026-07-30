@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: run-provider-lan-token.bat ^<provider_token^>
  exit /b 1
)
set WECHAT_UIA_PROVIDER_TOKEN=%~1
py -3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8070 --reload
