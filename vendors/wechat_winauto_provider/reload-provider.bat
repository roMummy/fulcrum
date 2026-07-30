@echo off
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Missing virtualenv python: .venv\Scripts\python.exe
  echo [HINT] Run setup-provider.bat first
  pause
  exit /b 1
)

if not exist "config.json" (
  echo [ERROR] Missing config.json
  pause
  exit /b 1
)

echo [INFO] Reloading provider config via http://127.0.0.1:8060/reload
".venv\Scripts\python.exe" -c "import json,urllib.request,pathlib;cfg=json.loads(pathlib.Path('config.json').read_text(encoding='utf-8'));req=urllib.request.Request('http://127.0.0.1:8060/reload',data=b'{}',method='POST');req.add_header('Content-Type','application/json');token=str(cfg.get('provider_token') or '').strip();req.add_header('X-Provider-Token',token) if token else None;print(urllib.request.urlopen(req,timeout=10).read().decode('utf-8'))"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Reload request failed with code %EXIT_CODE%
  pause
)

exit /b %EXIT_CODE%
