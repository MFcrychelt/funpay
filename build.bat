@echo off
rem ============================================================
rem  AutoStars - сборка .exe для Windows.
rem
rem  Результат в dist\ :
rem     AutoStarsBot.exe  - цикл выдачи + весь CLI  (PyInstaller, onefile)
rem     AutoStars.exe     - GUI                      (flet pack)
rem
rem  После сборки положите .env РЯДОМ с exe (можно скопировать .env.example).
rem  Пути к .env, config.json, autostars.db и autostars.log разрешаются
rem  относительно папки exe: сборку можно носить с собой на флешке.
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo.

if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

echo [1/3] зависимости для сборки ...
python -m pip install -q -e ".[gui,build]"
if errorlevel 1 (
    echo [ОШИБКА] не удалось поставить pyinstaller/flet - проверьте доступ в сеть.
    pause
    exit /b 1
)

echo.
echo [2/3] AutoStarsBot.exe ...
python -m PyInstaller --noconfirm --clean autostars.spec
if errorlevel 1 (
    echo [ОШИБКА] сборка CLI не удалась.
    pause
    exit /b 1
)

echo.
echo [3/3] AutoStars.exe (GUI) ...
where flet >nul 2>nul
if errorlevel 1 (
    echo [WARN] flet CLI не найден - GUI не упакован.
    echo        Запускайте его из папки: start_gui.bat
    goto done
)
flet pack autostars_gui.py -n AutoStars -i ico.ico --add-data .env.example:. -y
if errorlevel 1 (
    echo [WARN] flet pack вернул ошибку - см. вывод выше.
    echo        GUI работает и без упаковки: start_gui.bat
)

:done
echo.
echo Сборка завершена. Файлы в dist\ :
echo     dist\AutoStarsBot.exe   - двойной клик = выдача началась
echo     dist\AutoStars.exe      - интерфейс
echo.
echo Первый запуск на новой машине: проверьте окружение
echo     dist\AutoStarsBot.exe --check
echo.
pause
