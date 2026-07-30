@echo off
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"

call "%ROOT%setup-provider.bat"
if errorlevel 1 exit /b %ERRORLEVEL%

echo.
echo [INFO] Opening config.json for quick edit
call "%ROOT%open-config.bat"

echo.
echo [INFO] Starting provider
call "%ROOT%start-provider.bat"
exit /b %ERRORLEVEL%
