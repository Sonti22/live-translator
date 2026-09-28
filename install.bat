@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Installs (or updates) the built app into the user's Programs folder and creates shortcuts.
rem Keys, settings and records live next to the installed exe, so rebuilding dist\ never touches them.
if not exist "dist\LiveTranslator.exe" (
  echo Сначала соберите программу: build_exe.bat
  pause
  exit /b 1
)
set "TARGET=%LOCALAPPDATA%\Programs\Live Translator"
if not exist "%TARGET%" mkdir "%TARGET%"
copy /y "dist\LiveTranslator.exe" "%TARGET%\LiveTranslator.exe" >nul
if errorlevel 1 (
  echo Не удалось обновить программу: закройте Live Translator и запустите install.bat ещё раз.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$shell = New-Object -ComObject WScript.Shell;" ^
  "$places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'));" ^
  "foreach ($place in $places) {" ^
  "  $link = $shell.CreateShortcut((Join-Path $place 'Live Translator.lnk'));" ^
  "  $link.TargetPath = '%TARGET%\LiveTranslator.exe';" ^
  "  $link.WorkingDirectory = '%TARGET%';" ^
  "  $link.IconLocation = '%TARGET%\LiveTranslator.exe,0';" ^
  "  $link.Description = 'Live call translator in your own voice';" ^
  "  $link.Save() }"
if errorlevel 1 (
  echo Не удалось создать ярлык.
  pause
  exit /b 1
)
echo Готово: ярлык «Live Translator» на рабочем столе и в меню «Пуск».
echo Программа: %TARGET%
pause
