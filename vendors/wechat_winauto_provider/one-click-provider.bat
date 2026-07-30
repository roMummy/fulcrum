@echo off
setlocal

set "ROOT=%~dp0"
set "PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
cd /d "%ROOT%"

title WeChat WinAuto Provider Bootstrap

echo [INFO] WeChat WinAuto Provider one-click bootstrap
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found in PATH
  echo [HINT] Please install Python 3.11+ first and enable Add python.exe to PATH
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment
    pause
    exit /b 1
  )
) else (
  echo [INFO] Virtual environment already exists
)

echo [INFO] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip -i %PIP_INDEX_URL%
if errorlevel 1 (
  echo [ERROR] Failed to upgrade pip
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install -i %PIP_INDEX_URL% -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Failed to install requirements
  pause
  exit /b 1
)

if not exist "config.json" (
  echo [INFO] Creating config.json from template...
  copy /Y "config.example.json" "config.json" >nul
) else (
  echo [INFO] config.json already exists
)

echo.
echo [INFO] Opening config.json for edit
start "" notepad "%ROOT%config.json"

echo.
echo [INFO] When you finish editing config.json, press any key here to continue startup...
pause >nul

echo.
echo [INFO] Starting wechat_winauto_provider on http://127.0.0.1:8060
echo [INFO] Press Ctrl+C to stop
echo.

".venv\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8060
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Provider exited with code %EXIT_CODE%
  pause
)

exit /b %EXIT_CODE%
