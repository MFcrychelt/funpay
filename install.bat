@echo off
rem ============================================================
rem  AutoStars - установка на Windows: venv + зависимости + проверка.
rem  Запустите один раз. Дальше: start_gui.bat или run_bot.bat
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo.
echo === AutoStars: установка ===
echo.

set "PYCMD=python"
where py >nul 2>nul && set "PYCMD=py -3"

%PYCMD% --version >nul 2>nul
if errorlevel 1 (
    echo [ОШИБКА] Python не найден в PATH.
    echo Установите Python 3.10+ с python.org и включите "Add python.exe to PATH".
    pause
    exit /b 1
)

%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ОШИБКА] Нужен Python 3.10 или новее. Текущий:
    %PYCMD% --version
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Создаю виртуальное окружение .venv ...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось создать venv.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

echo Ставлю зависимости: ядро + GUI ...
python -m pip install --upgrade pip >nul
python -m pip install -e ".[gui]"
if errorlevel 1 (
    echo.
    echo [ОШИБКА] pip install не удался. Частые причины: антивирус или файервол
    echo блокирует PyPI, нет доступа в сеть. Ядро можно поставить отдельно:
    echo     python -m pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Создан .env из примера. Откройте его и заполните:
    echo     FUNPAY_GOLDEN_KEY, GAMEAU_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    echo Можно и позже - через вкладку "Настройки" в интерфейсе.
)

echo.
echo Проверка окружения:
python check_env.py
echo.
echo Готово.
echo     GUI              start_gui.bat
echo     цикл выдачи      run_bot.bat
echo     диагностика      run_bot.bat --check
echo.
pause
