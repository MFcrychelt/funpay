@echo off
rem ============================================================
rem  AutoStars - графический интерфейс.
rem  GUI управляет тем же движком, что и консоль: запускает цикл
rem  автовыдачи отдельным процессом, читает ту же базу и .env.
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo [WARN] .venv не найден - использую системный Python.
    echo        Рекомендуется сначала запустить install.bat
    echo        или: python -m pip install -e ".[gui]"
)

set PYTHONIOENCODING=utf-8
python autostars_gui.py
if errorlevel 1 (
    echo.
    echo Если не найден модуль flet, поставьте GUI-зависимости:
    echo     python -m pip install -e ".[gui]"
    echo.
    pause
)
