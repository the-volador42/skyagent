@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if exist "desktop\node_modules\electron\dist\electron.exe" (
  start "" "desktop\node_modules\electron\dist\electron.exe" "%~dp0desktop"
) else (
  echo Electron не установлен - открываю в браузере. Для приложения: cd desktop и npm install
  "%PY%" web.py %*
)
