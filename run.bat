@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [Gatecheck] .venv не найден - создаю окружение и ставлю зависимости...
    python -m venv .venv
    if errorlevel 1 goto :err
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
    if errorlevel 1 goto :err
)

if not exist ".env" (
    echo [Gatecheck] Файла .env нет. Скопируй .env.example в .env и впиши BOT_TOKEN.
    copy .env.example .env
    echo [Gatecheck] Создан шаблон .env - впиши BOT_TOKEN и запусти снова.
    exit /b 2
)

".venv\Scripts\python.exe" main.py %*
exit /b %errorlevel%

:err
echo [Gatecheck] Ошибка создания окружения или установки зависимостей.
exit /b 1
