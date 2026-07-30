@echo off
setlocal
cd /d "%~dp0"
py -3.11 -m uvicorn app.main:app --host 127.0.0.1 --port 8070 --reload
