@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0..\.aetherloom-venv\Scripts\python.exe" (
    "%~dp0..\.aetherloom-venv\Scripts\python.exe" "%~dp0AetherLoom.py"
) else (
    python "%~dp0AetherLoom.py"
)
if errorlevel 1 pause
