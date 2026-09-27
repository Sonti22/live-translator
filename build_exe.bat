@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem pip can't use a SOCKS system proxy on its own; PyPI is reachable directly
set NO_PROXY=*
py -3 -m pip install -q -r requirements.txt pyinstaller || goto :error
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name LiveTranslator ^
  --collect-data soundcard gui.py || goto :error
echo.
echo Готово: dist\LiveTranslator.exe
pause
exit /b 0
:error
echo Сборка не удалась.
pause
exit /b 1
