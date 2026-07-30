@echo off
setlocal
cd /d "%~dp0"
py -3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8070 --reload
