@echo off
setlocal
cd /d "%~dp0"
echo [INFO] Setting up workwechat_provider
set "PY_BOOTSTRAP="
where py >nul 2>nul
if not errorlevel 1 set "PY_BOOTSTRAP=py -3"
if "%PY_BOOTSTRAP%"=="" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python not found. Install Python or add it to PATH first.
    exit /b 1
  )
  set "PY_BOOTSTRAP=python"
)
if not exist ".venv\Scripts\python.exe" (
  rem 创建 provider 虚拟环境；优先使用 py -3，没有 py 启动器时回退到 python。
  %PY_BOOTSTRAP% -m venv .venv
  if errorlevel 1 exit /b %ERRORLEVEL%
)
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Missing virtualenv python after setup: .venv\Scripts\python.exe
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b %ERRORLEVEL%
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b %ERRORLEVEL%
if not exist "config.json" copy "config.example.json" "config.json" >nul
".venv\Scripts\python.exe" -c "import json,pathlib;cfg=json.loads(pathlib.Path('config.json').read_text(encoding='utf-8'));print(cfg.get('hook_python_executable','').strip())" > .hook_python_path.tmp
set /p HOOK_PYTHON=<.hook_python_path.tmp
del .hook_python_path.tmp >nul 2>nul
if not "%HOOK_PYTHON%"=="" (
  if exist "%HOOK_PYTHON%" (
    rem 给内置 Hook runtime 使用的 32 位 Python 安装依赖；真实消息推送需要 requests。
    "%HOOK_PYTHON%" -m pip install -r hook_runtime\requirements.txt
    if errorlevel 1 exit /b %ERRORLEVEL%
  ) else (
    echo [WARN] hook_python_executable not found: %HOOK_PYTHON%
  )
)
echo [INFO] Done. Edit config.json before starting.
