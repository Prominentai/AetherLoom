@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "AETHERLOOM_RUN_PYTHON=python"
if not "%~1"=="" set "AETHERLOOM_RUN_PYTHON=%~f1"
pushd "%~dp0" || exit /b 1
"%AETHERLOOM_RUN_PYTHON%" "%~dp0AetherLoom.py"
set "AETHERLOOM_RUN_EXIT=%errorlevel%"
popd
if not "%AETHERLOOM_RUN_EXIT%"=="0" if not defined AETHERLOOM_NO_PAUSE pause
exit /b %AETHERLOOM_RUN_EXIT%
