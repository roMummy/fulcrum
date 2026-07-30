@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [HINT] Run setup-provider.bat first
  exit /b 1
)
rem 默认回退端口使用 8181，避免旧端口落入 Windows TCP 排除端口段。
".venv\Scripts\python.exe" -c "import json,urllib.request,pathlib;cfg=json.loads(pathlib.Path('config.json').read_text(encoding='utf-8'));req=urllib.request.Request('http://127.0.0.1:%s/reload' % cfg.get('provider_port',8181),data=b'{}',method='POST');token=str(cfg.get('provider_token') or '').strip();req.add_header('X-Provider-Token',token) if token else None;print(urllib.request.urlopen(req,timeout=10).read().decode('utf-8'))"
