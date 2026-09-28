@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem pip can't use a SOCKS system proxy on its own; PyPI is reachable directly
set NO_PROXY=*
rem every time: a no-op when all is installed, and it upgrades what is too old (websockets < 15)
py -3 -m pip install -q -r requirements.txt || goto :error
rem the app must see the Windows (VPN) proxy: any *_PROXY variable would hide it from Python
set NO_PROXY=
py -3 live_translator.py %*
pause
exit /b 0

:error
echo Не удалось подготовить Python-пакеты — сообщение об ошибке выше.
pause
exit /b 1
