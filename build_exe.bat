@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 3.11+: the exe carries this Python, and on 3.10 one slow websocket handshake ends the call
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" || goto :old_python
rem pip can't use a SOCKS system proxy on its own; PyPI is reachable directly
set NO_PROXY=*
py -3 -m pip install -q -r requirements.txt pyinstaller || goto :error
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name LiveTranslator --icon icon.ico ^
  --add-data "ui;ui" --collect-data soundcard --collect-all webview app.py || goto :error
echo.
echo Готово: dist\LiveTranslator.exe
pause
exit /b 0
:error
echo Сборка не удалась.
pause
exit /b 1
:old_python
echo Нужен Python 3.11 или новее: python.org/downloads
pause
exit /b 1
