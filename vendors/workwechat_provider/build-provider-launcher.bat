@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem WorkWechat hook DLL is 32-bit, so provider/server/runtime must be built with x86 Python.
rem Resolve Python from explicit PY32, local portable Python, existing x86 venv, common install paths, then py.exe.
rem This keeps build hosts without the Windows py launcher usable.
if "%PY32%"=="" (
  if exist ".python310-32\python.exe" (
    set "PY32=.python310-32\python.exe"
  ) else if exist ".venv32\Scripts\python.exe" (
    set "PY32=.venv32\Scripts\python.exe"
  ) else if exist "%LOCALAPPDATA%\Programs\Python\Python310-32\python.exe" (
    set "PY32=%LOCALAPPDATA%\Programs\Python\Python310-32\python.exe"
  ) else if exist "%ProgramFiles(x86)%\Python310-32\python.exe" (
    set "PY32=%ProgramFiles(x86)%\Python310-32\python.exe"
  ) else (
    set "PY32=py -3.10-32"
  )
)
%PY32% -c "import struct; raise SystemExit(0 if struct.calcsize('P') * 8 == 32 else 1)"
if errorlevel 1 (
  echo [ERROR] Python 3.10 32-bit is required. Install it, create .python310-32, or set PY32 to a 32-bit python.exe.
  exit /b 1
)

if not exist ".venv32\Scripts\python.exe" (
  rem Keep a dedicated x86 venv so the main 64-bit project environment is not reused by accident.
  %PY32% -m venv .venv32
  if errorlevel 1 exit /b %ERRORLEVEL%
)

".venv32\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b %ERRORLEVEL%
".venv32\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b %ERRORLEVEL%
".venv32\Scripts\python.exe" -m pip install -r hook_runtime\requirements.txt
if errorlevel 1 exit /b %ERRORLEVEL%

rem Generate the Windows exe icon from the same vector-style drawing used by the launcher UI.
".venv32\Scripts\python.exe" generate_launcher_icon.py
if errorlevel 1 exit /b %ERRORLEVEL%

rem Clean previous artifacts so stale exe files or _internal folders do not leak into this package.
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

rem Build internal provider server and hook runtime first; the final UI exe embeds both of them.
".venv32\Scripts\pyinstaller.exe" --noconsole --onefile --name WorkWechatProviderServer app.py
if errorlevel 1 exit /b %ERRORLEVEL%
".venv32\Scripts\pyinstaller.exe" --noconsole --onefile --name WorkWechatHookRuntime hook_runtime\http_py.py
if errorlevel 1 exit /b %ERRORLEVEL%

rem The customer-facing package is one UI exe. It extracts server/runtime resources at launch.
rem websocket-client is imported inside the cloud WS loop, so keep the hidden import explicit for PyInstaller.
rem Keep this command on one line because cmd can parse caret continuations differently across environments.
".venv32\Scripts\pyinstaller.exe" --noconsole --onefile --name WorkWechatProvider --icon "assets\workwechat-provider.ico" --hidden-import websocket --add-binary "dist\WorkWechatProviderServer.exe;." --add-binary "dist\WorkWechatHookRuntime.exe;hook_runtime" --add-binary "hook_runtime\Loader.dll;hook_runtime" --add-binary "hook_runtime\WeChat_5.0.0.6008.dll;hook_runtime" --add-data "hook_runtime\API.docx;hook_runtime" --add-data "config.example.json;." launcher_app.py
if errorlevel 1 exit /b %ERRORLEVEL%

rem Remove temporary internal exe files so dist exposes only the UI entry point.
if exist "dist\WorkWechatProviderServer.exe" del /q "dist\WorkWechatProviderServer.exe"
if exist "dist\WorkWechatHookRuntime.exe" del /q "dist\WorkWechatHookRuntime.exe"

echo [INFO] Done: dist\WorkWechatProvider.exe
echo [INFO] Customer flow: open WorkWechat, then run WorkWechatProvider.exe.
