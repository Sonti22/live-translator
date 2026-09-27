@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -c "import sounddevice, soundcard, numpy, websockets, python_socks" 2>nul || (
  rem pip can't use a SOCKS system proxy on its own; PyPI is reachable directly
  set NO_PROXY=*
  py -3 -m pip install -q -r requirements.txt
)
py -3 live_translator.py %*
pause
