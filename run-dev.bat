@echo off
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Missing virtualenv python: .venv\Scripts\python.exe
  pause
  exit /b 1
)

if not exist ".run\launch_uvicorn.py" (
  echo [ERROR] Missing launcher: .run\launch_uvicorn.py
  pause
  exit /b 1
)

rem SaaS 主服务使用独立 8280，避免与原 fulcrum 的 8180 冲突。
rem 启动前先按端口释放旧的开发服务进程，避免上一次 Ctrl+C 或窗口关闭后 uvicorn 子进程仍占用端口。
rem 默认只处理主服务端口，防止误杀正在单独调试的 workwechat provider；如需一起清理可设置：
rem   set RUN_DEV_EXTRA_PORTS=8181 8989
set "MAIN_PORT=8280"
echo [INFO] Releasing dev ports before startup...
call :KillPort "%MAIN_PORT%"
for %%P in (%RUN_DEV_EXTRA_PORTS%) do call :KillPort "%%~P"
ping -n 2 127.0.0.1 >nul

if "%RUN_DEV_DRY_RUN%"=="1" (
  echo [INFO] RUN_DEV_DRY_RUN=1, skip server startup
  exit /b 0
)

echo [INFO] Starting fulcrum on http://127.0.0.1:%MAIN_PORT%
echo [INFO] Press Ctrl+C to stop
echo.

".venv\Scripts\python.exe" ".run\launch_uvicorn.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Server exited with code %EXIT_CODE%
  pause
)

exit /b %EXIT_CODE%

:KillPort
set "PORT=%~1"
if "%PORT%"=="" exit /b 0
set "FOUND_PORT_PID="

rem netstat 只查 LISTENING 状态的 TCP 端口；taskkill 使用 /T 一并结束 uvicorn 拉起的子进程树。
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%PORT% .*LISTENING"') do (
  if not "%%P"=="0" (
    set "FOUND_PORT_PID=1"
    echo [INFO] Releasing port %PORT% from PID %%P
    taskkill /PID %%P /T /F >nul 2>nul
    if errorlevel 1 (
      echo [WARN] Failed to kill PID %%P for port %PORT%. You may need admin privileges.
    ) else (
      echo [INFO] Killed PID %%P for port %PORT%
    )
  )
)

if "%FOUND_PORT_PID%"=="" echo [INFO] Port %PORT% is free
exit /b 0
