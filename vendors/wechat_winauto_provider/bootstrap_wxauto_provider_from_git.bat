@echo off
setlocal

title Bootstrap WeChat WinAuto Provider From Git

set "SCRIPT_DIR=%~dp0"
set "REPO_URL=http://192.168.2.32:3000/openclaw/multi-bot-admin.git"
set "REPO_BRANCH=main"
set "TARGET_ROOT=%SCRIPT_DIR%"
set "REPO_DIR=%TARGET_ROOT%\multi-bot-admin"
set "PROVIDER_DIR=%REPO_DIR%\vendors\wechat_winauto_provider"

echo [INFO] Bootstrap WeChat WinAuto Provider From Git
echo [INFO] Repo URL: %REPO_URL%
echo [INFO] Branch  : %REPO_BRANCH%
echo [INFO] Target  : %REPO_DIR%
echo.

where git >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Git was not found in PATH
  echo [HINT] Please install Git for Windows first
  pause
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found in PATH
  echo [HINT] Please install Python 3.11+ first and enable Add python.exe to PATH
  pause
  exit /b 1
)

if not exist "%TARGET_ROOT%" (
  mkdir "%TARGET_ROOT%"
)

if exist "%REPO_DIR%\.git" (
  echo [INFO] Existing git repository found, updating...
  cd /d "%REPO_DIR%"
  git fetch origin
  if errorlevel 1 (
    echo [ERROR] git fetch failed
    pause
    exit /b 1
  )
  git checkout %REPO_BRANCH%
  if errorlevel 1 (
    echo [ERROR] git checkout failed
    pause
    exit /b 1
  )
  git pull origin %REPO_BRANCH%
  if errorlevel 1 (
    echo [ERROR] git pull failed
    pause
    exit /b 1
  )
) else (
  echo [INFO] Cloning repository...
  cd /d "%TARGET_ROOT%"
  git clone --branch %REPO_BRANCH% "%REPO_URL%" "%REPO_DIR%"
  if errorlevel 1 (
    echo [ERROR] git clone failed
    pause
    exit /b 1
  )
)

if not exist "%PROVIDER_DIR%\one-click-provider.bat" (
  echo [ERROR] Provider bootstrap script not found:
  echo         %PROVIDER_DIR%\one-click-provider.bat
  pause
  exit /b 1
)

echo.
echo [INFO] Launching provider one-click installer...
cd /d "%PROVIDER_DIR%"
call "%PROVIDER_DIR%\one-click-provider.bat"
exit /b %ERRORLEVEL%
