@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title SkyAgent

rem Модель агента (и по умолчанию — сводок и наблюдателя). Можно задать свою: set SKYAGENT_MODEL=...
if not defined SKYAGENT_MODEL set "SKYAGENT_MODEL=gemma4:12b-it-q4_K_M"

if not exist ".venv\Scripts\python.exe" (
  echo [!] Не найдено окружение .venv. Создай его: uv venv .venv  и  uv pip install -r requirements.txt
  pause
  exit /b 1
)

rem --- Ollama: если не отвечает — запускаем и ждём до 30 секунд
curl -s -o nul http://127.0.0.1:11434/api/version
if errorlevel 1 (
  echo Запускаю Ollama...
  start "Ollama" /min ollama serve
  for /l %%i in (1,1,30) do (
    timeout /t 1 /nobreak >nul
    curl -s -o nul http://127.0.0.1:11434/api/version && goto ollama_ok
  )
  echo [!] Ollama не запустилась. Проверь, что она установлена.
  pause
  exit /b 1
)
:ollama_ok

rem --- Neo4j (граф памяти): необязателен — без него память работает без графа
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\neo4j.ps1" start

rem --- SkyAgent CLI. Дополнительные параметры передаются дальше, например: SkyAgentCLI.bat --live --no-mic
rem --- Мини-окно в углу экрана открывается вместе с CLI. Без него: set SKYAGENT_MINI=0
set "MINI=--mini"
if "%SKYAGENT_MINI%"=="0" set "MINI="
".venv\Scripts\python.exe" cli.py --model %SKYAGENT_MODEL% %MINI% %*
if errorlevel 1 pause
endlocal
