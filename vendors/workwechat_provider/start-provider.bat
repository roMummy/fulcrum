@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [HINT] Run setup-provider.bat first
  exit /b 1
)
rem workwechat provider 独立监听 8181，和主服务 8180、底层 Hook 8989 分开。
echo [INFO] Starting workwechat_provider on http://127.0.0.1:8181
".venv\Scripts\python.exe" app.py
exit /b %ERRORLEVEL%
