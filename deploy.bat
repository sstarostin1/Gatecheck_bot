@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

rem Обёртка над scripts/deploy.py: деплой Gatecheck Bot на прод с проверками.
rem Примеры:  deploy.bat --dry-run   ·   deploy.bat   ·   deploy.bat --strict-errors
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

"%PY%" "%~dp0scripts\deploy.py" %*
exit /b %errorlevel%
