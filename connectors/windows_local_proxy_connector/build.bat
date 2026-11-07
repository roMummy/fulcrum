@echo off
setlocal
py -3.11 -m pip install -r requirements.txt pyinstaller
py -3.11 -m PyInstaller --noconsole --onefile --name MultiBotLocalProxyConnector main.py
echo Build finished: dist\MultiBotLocalProxyConnector.exe
