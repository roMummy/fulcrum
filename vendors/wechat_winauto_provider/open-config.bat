@echo off
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist "config.json" (
  if exist "config.example.json" (
    copy /Y "config.example.json" "config.json" >nul
    echo [INFO] Created config.json from template
  ) else (
    echo [ERROR] Missing config.example.json
    pause
    exit /b 1
  )
)

start "" notepad "%ROOT%config.json"
exit /b 0
