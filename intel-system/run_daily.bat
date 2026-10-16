@echo off
REM =========================================================
REM  Intel System - daily collection launcher
REM
REM  Portable version: resolves paths relative to this script,
REM  so the project can live anywhere.
REM
REM  NOTE: keep this file ASCII-only. cmd.exe reads .bat files in
REM  the system ANSI codepage; non-ASCII text here becomes garbage
REM  commands and the script fails.
REM
REM  NOTE: this file must use CRLF line endings. cmd.exe does not
REM  handle LF-only batch files correctly.
REM =========================================================

setlocal

REM Project root = directory containing this script
set "PROJ=%~dp0"
if "%PROJ:~-1%"=="\" set "PROJ=%PROJ:~0,-1%"

set "LOGDIR=%PROJ%\data\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM Log file name: yyyyMMdd
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "D=%%i"
set "LOG=%LOGDIR%\run_%D%.log"

cd /d "%PROJ%"

echo ============================================>> "%LOG%"
echo [START] %date% %time%>> "%LOG%"
echo ============================================>> "%LOG%"

REM Use the same interpreter that runs this project's dependencies.
REM Override by setting PYTHON_EXE before running, or by editing here.
if defined PYTHON_EXE (
    set "PY=%PYTHON_EXE%"
) else (
    set "PY=python"
)

%PY% scripts\run_all.py >> "%LOG%" 2>&1
set "CODE=%ERRORLEVEL%"

%PY% scripts\sync_reports.py >> "%LOG%" 2>&1
%PY% scripts\make_report_brief.py 20 >> "%LOG%" 2>&1
%PY% scripts\refresh_sources.py 3 >> "%LOG%" 2>&1

echo [END] %date% %time% exit=%CODE%>> "%LOG%"

REM Keep 30 days of logs
forfiles /p "%LOGDIR%" /m *.log /d -30 /c "cmd /c del @path" >nul 2>&1

endlocal
