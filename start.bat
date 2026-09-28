@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 3.11+: on 3.10 a slow websocket handshake raises asyncio.TimeoutError, which is no OSError there and ends the call
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" || goto :old_python
rem pip can't use a SOCKS system proxy on its own; PyPI is reachable directly
set NO_PROXY=*
rem every time: a no-op when all is installed, and it upgrades what is too old (websockets < 15)
py -3 -m pip install -q -r requirements.txt || goto :error
rem the app must see the Windows (VPN) proxy: any *_PROXY variable would hide it from Python
set NO_PROXY=
py -3 -c "import app" || goto :error
start "" pyw -3 app.py %*
exit /b 0

:error
echo Не удалось подготовить Python-пакеты — сообщение об ошибке выше.
pause
exit /b 1

:old_python
echo Нужен Python 3.11 или новее: python.org/downloads
pause
exit /b 1
