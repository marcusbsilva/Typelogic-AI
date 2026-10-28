@echo off
rem TypeLogic AI launcher. Double-click for the menu, or: TypeLogic.bat start --mode cpu|gpu|hybrid, setup, models, verify,
rem cli ..., settings, evaluate, diagnose, test, build-native. See README.md.
setlocal
cd /d "%~dp0"
set "TL_PY="
if exist "runtime\windows\python.exe" set "TL_PY=runtime\windows\python.exe"
if not defined TL_PY if exist ".venv\Scripts\python.exe" set "TL_PY=.venv\Scripts\python.exe"
if not defined TL_PY (py -3.12 -c "import sys" >nul 2>&1 && set "TL_PY=py -3.12")
if not defined TL_PY (py -3 -c "import sys" >nul 2>&1 && set "TL_PY=py -3")
if not defined TL_PY (python -c "import sys" >nul 2>&1 && set "TL_PY=python")
if not defined TL_PY (
  echo Python 3.12 was not found. Install it from https://www.python.org/downloads/ and run TypeLogic.bat again.
  pause
  exit /b 1
)
%TL_PY% "%~dp0launcher\launcher.py" %*
set "TL_CODE=%errorlevel%"
if not "%TL_CODE%"=="0" if not "%~1"=="" pause
exit /b %TL_CODE%
