@echo off
setlocal

set "ROOT=%~dp0"
set "PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
cd /d "%ROOT%"

echo [INFO] Setting up wechat_winauto_provider

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found in PATH
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
echo [OK] Setup completed
echo [NEXT] Edit config.json before first real start if needed
pause
exit /b 0
