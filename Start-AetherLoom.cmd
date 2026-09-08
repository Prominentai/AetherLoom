@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "AETHERLOOM_RUN_PYTHON=python"
if exist "%~dp0..\.aetherloom-venv\Scripts\python.exe" set "AETHERLOOM_RUN_PYTHON=%~dp0..\.aetherloom-venv\Scripts\python.exe"
if exist "%~dp0.venv\Scripts\python.exe" set "AETHERLOOM_RUN_PYTHON=%~dp0.venv\Scripts\python.exe"
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" set "AETHERLOOM_RUN_PYTHON=%VIRTUAL_ENV%\Scripts\python.exe"
if not "%~1"=="" set "AETHERLOOM_RUN_PYTHON=%~1"
rem Resolve a relative interpreter before changing the working directory.
if not "%~1"=="" if exist "%~1" set "AETHERLOOM_RUN_PYTHON=%~f1"
pushd "%~dp0" || exit /b 1
"%AETHERLOOM_RUN_PYTHON%" "%~dp0AetherLoom.py"
set "AETHERLOOM_RUN_EXIT=%errorlevel%"
popd
if not "%AETHERLOOM_RUN_EXIT%"=="0" if not defined AETHERLOOM_NO_PAUSE pause
exit /b %AETHERLOOM_RUN_EXIT%
