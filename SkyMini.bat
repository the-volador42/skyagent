@echo off
rem Мини-окно SkyAgent (без консоли). Агент должен быть запущен: SkyAgentCLI.bat
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (start "" ".venv\Scripts\pythonw.exe" mini.py) else (start "" pythonw mini.py)
