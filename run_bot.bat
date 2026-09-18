@echo off
rem ============================================================
rem  AutoStars - цикл автовыдачи (он же CLI)
rem  Примеры:
rem     run_bot.bat
rem     run_bot.bat --check
rem     run_bot.bat --stats
rem     run_bot.bat --report
rem     run_bot.bat --pause   /  run_bot.bat --resume
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

python autostars_bot.py %*
if errorlevel 1 pause
