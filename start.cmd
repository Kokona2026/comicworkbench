@echo off
REM One-click entry: start ComfyUI (background), run the audit, open the console.
REM Extra arguments are forwarded, e.g.:  start.cmd --no-browser
REM                                        start.cmd --port 8800

chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo.
    echo [FAIL] Python 3.9+ not found. Install Python and re-run start.cmd.
    echo.
    pause
    exit /b 1
)

%PY% "app\server.py" --with-comfyui %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [FAIL] console exited with code %RC%. See reports\audit.log
    pause
)
exit /b %RC%
