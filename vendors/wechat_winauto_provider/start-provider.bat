@echo off
setlocal

set "ROOT=%~dp0"
set "RELOAD_DIR=%ROOT:~0,-1%"
cd /d "%ROOT%"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Missing virtualenv python: .venv\Scripts\python.exe
  echo [HINT] Run setup-provider.bat first
  pause
  exit /b 1
)

if not exist "config.json" (
  echo [WARN] config.json not found, creating from config.example.json
  copy /Y "config.example.json" "config.json" >nul
)

echo [INFO] Starting wechat_winauto_provider on http://127.0.0.1:8060
echo [INFO] Dev hot reload is enabled
echo [INFO] Press Ctrl+C to stop
echo.

".venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8060 --reload --reload-dir "%RELOAD_DIR%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Provider exited with code %EXIT_CODE%
  pause
)

exit /b %EXIT_CODE%
