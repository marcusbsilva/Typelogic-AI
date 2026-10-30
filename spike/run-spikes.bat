@echo off
setlocal
rem TypeLogic v2 spikes, with the Python environments of TypeLogic 1.x (nothing is installed).
rem   run-spikes.bat            fetch, convert, reference, bench and report (resumes; finished steps are skipped)
rem   run-spikes.bat bench --only spec-q8,clef-flash-q4
rem TYPELOGIC_V1 overrides the 1.x folder (default: TypeLogic-AI next to TypeLogic-V2).
rem The GPU environment of 1.x (%USERPROFILE%\TypeLogic-XPU, "TypeLogic.bat setup --gpu") is preferred: the PyTorch
rem reference then runs on the Intel Arc GPU like the 1.x application; otherwise it runs on the CPU (slower).

if "%TYPELOGIC_V1%"=="" set "TYPELOGIC_V1=%~dp0..\..\TypeLogic-AI"
set "PY=%USERPROFILE%\TypeLogic-XPU\Scripts\python.exe"
if not exist "%PY%" set "PY=%TYPELOGIC_V1%\runtime\windows\python.exe"
if not exist "%PY%" (
  echo No TypeLogic 1.x Python found. Set TYPELOGIC_V1 to the TypeLogic 1.x folder.
  exit /b 1
)
set PYTHONUTF8=1
set PYTHONNOUSERSITE=1
set HF_HUB_DISABLE_TELEMETRY=1
set DO_NOT_TRACK=1
"%PY%" "%~dp0spike.py" %*
set RC=%ERRORLEVEL%
echo.
if %RC%==0 (echo Spikes finished. Report: "%~dp0results\REPORT.md") else (echo Spikes stopped with code %RC%. Log: "%~dp0results\spike.log")
pause
exit /b %RC%
