@echo off
REM Audit only: no browser, no ComfyUI start-up. Writes reports/audit-latest.json
REM and docs/05-audit.md. Extra arguments are forwarded, e.g.: audit.cmd --deep

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
    echo [FAIL] Python 3.9+ not found.
    echo.
    pause
    exit /b 1
)

%PY% "app\audit_engine.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [FAIL] audit exited with code %RC%.
    pause
)
exit /b %RC%
