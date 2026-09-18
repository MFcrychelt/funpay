# 🪟 Windows: установка, запуск, сборка `.exe`

## 1. Что где лежит (важно понять до первого запуска)

| Сценарий | Папка конфигурации/данных (`app_dir`) | Откуда берутся примеры (`resource_dir`) |
|---|---|---|
| из исходников | корень проекта (`funpay\`) | там же |
| `AutoStarsBot.exe` (one-file) | папка, **где лежит exe** | временный каталог `_MEIPASS` (`.env.example` внутри архива) |
| `AUTOSTARS_HOME` задана | указанная папка | — |

Реализация — [`autostars/paths.py`](../autostars/paths.py). Смысл: двойной клик по
exe имеет рабочей папкой `C:\Windows\System32` (или `profile`), поэтому `Path.cwd()`
в коде не используется нигде — иначе «бот создаёт базу непонятно где».

Рядом с exe (или в `AUTOSTARS_HOME`) должны лежать: `.env`, `autostars.db`,
`autostars.log`. Первый запуск сам копирует `.env.example` → `.env` и просит
заполнить ключи (и возвращает **ненулевой** код, чтобы скрипты не приняли это за успех).

## 2. Установка без сборки (рекомендуемый путь)

```bat
git clone https://github.com/MFcrychelt/funpay.git
cd funpay
install.bat
```

Что делает `install.bat`:

1. ищет `py -3`, иначе `python`; проверяет версию ≥ 3.10 (иначе — понятная ошибка);
2. `python -m venv .venv` и `call .venv\Scripts\activate.bat`;
3. `pip install --upgrade pip` и `pip install -e ".[gui]"` (движок + flet);
4. `copy .env.example .env`, если своего `.env` ещё нет;
5. `python check_env.py` — Python, версии пакетов, пути, наличие ключей, права на запись;
6. подсказывает `start_gui.bat` / `run_bot.bat`.

Сеть к PyPI блокируется антивирусом/прокси — ставьте вручную
`python -m pip install -e ".[gui]" --proxy=http://…` или хотя бы ядро:
`pip install -r requirements.txt`.

## 3. Запуск

| Файл | Команда | Что происходит |
|---|---|---|
| `start_gui.bat` | двойной клик | `.venv\Scripts\python.exe autostars_gui.py` — окно интерфейса (не `flet run`: dev-режим hot reload не нужен пользователю и требует flet-cli) |
| `run_bot.bat` | двойной клик | `python autostars_bot.py` — цикл выдачи в консоли |
| `run_bot.bat --check` | | диагностика |
| `run_bot.bat --once -v` | | один проход с отладочным логом |
| `run_bot.bat --pause` / `--resume` | | пауза приёма заказов (флаг в БД) |

GUI управляет циклом как **отдельным процессом**: закрытие окна не останавливает
выдачу, «⏹ Остановить» отправляет SIGTERM → graceful shutdown (ожидание начатых
покупок до `SHUTDOWN_TIMEOUT_SEC`).

## 4. Сборка `.exe`

```bat
build.bat
```

Шаги:

1. `pip install -e ".[gui,build]"` (pyinstaller + flet);
2. `python -m PyInstaller --noconfirm --clean autostars.spec` → `dist\AutoStarsBot.exe`;
3. `flet pack autostars_gui.py -n AutoStars -i ico.ico --add-data .env.example:. -y`
   → `dist\AutoStars.exe` (если `flet` в PATH нет — сборка CLI не отменяется,
   скрипт предупреждает и предлагает `start_gui.bat`).

Почему две утилиты: `autostars.spec` делает CLI (console, onefile, `excludes`
`flet/numpy/pandas/…`), а GUI без Flutter-рантайма не работает — его умеет
корректно упаковывать только `flet pack`.

### Что внутри spec

- `datas`: `.env.example` (+ `config.example.json`, если есть) — чтобы «первый
  запуск создал `.env`» работал и в exe;
- `hiddenimports`: `aiosqlite`, `bs4`, `httpx._transports.default`, `autostars.main`,
  `autostars.services.bot_control` — их PyInstaller не всегда видит сам;
- `console=True`: это консольная утилита; для «без окна» используйте GUI или
  `pythonw`/`.bat` с `start /min`;
- `upx=False`: UPX часто даёт ложные срабатывания антивирусов;
- `icon=ico.ico`, если файл лежит в корне проекта.

### Данные в дистрибутив не попадают

`.env`, `autostars.db`, `autostars.log` **не** включаются в exe (в этом и смысл
`app_dir()`): сборку можно копировать на другую машину, конфиг кладётся рядом.
Проверьте, что в `build\` и `dist\` не осталось `.env` с ключами, прежде чем
отдавать архив.

## 5. Проверка собранного exe

```bat
dist\AutoStarsBot.exe --version
dist\AutoStarsBot.exe --check
dist\AutoStarsBot.exe --calc 1370.4 1000
dist\AutoStarsBot.exe --stats --json
start "" "dist\AutoStars.exe"
```

Ожидаемое поведение при отсутствии `.env`: создан `.env` рядом с exe, сообщение
«заполните ключи», код возврата 1. Дальше — `notepad .env`, повторить `--check`,
потом запуск цикла (или кнопка «▶ Запустить цикл» в GUI).

Минимальный набор файлов, если хочется «портативную папку»:

```
AutoStars\
  AutoStarsBot.exe
  AutoStars.exe
  .env
  autostars.db      (создаётся сам)
  autostars.log     (создаётся сам)
```

## 6. Автозапуск

**Вариант 1 — планировщик** (логин пользователя, GUI-сессия не нужна):

```bat
schtasks /Create /TN AutoStars /SC ONSTART /RU SYSTEM ^
  /TR "\"C:\autostars\dist\AutoStarsBot.exe\""
```

**Вариант 2 — папка «Автозагрузка»** (`Win+R` → `shell:startup` → ярлык на
`start_gui.bat`): окно появляется после входа в систему, цикл стартует кнопкой
«▶ Запустить цикл» (или аргументом `run_bot.bat`, если окно не нужно).

**Вариант 3 — NSSM как служба**: `nssm install AutoStars C:\autostars\dist\AutoStarsBot.exe`,
`nssm set AutoStars AppDirectory C:\autostars\dist`, `nssm set AutoStars AppExit Default Restart`.
Graceful shutdown работает и тут: служба получает stop → SIGTERM-подобная остановка,
`AppStopMethodConsole`/`WinEvent` с таймаутом > `SHUTDOWN_TIMEOUT_SEC`.

## 7. Проблемы и решения

| Симптом | Причина / решение |
|---|---|
| «Не является приложением для Windows» / сразу закрывается | запуск из `\\tsclient\…` или с флешки с NTFS-ограничениями; скопируйте папку на диск, `Unblock` в свойствах файла |
| SmartScreen «Система защитила ваш компьютер» | нет подписи кода → «Подробнее» → «Выполнить в любом случае»; для регулярного использования держите сборку из `build.bat` сами, а не скачанную |
| Антивирус удаляет exe | ложное срабатывание на PyInstaller one-file: добавьте папку в исключения, соберите с `--onedir` (в spec `onefile=False`) — файлы в `dist\AutoStarsBot\` |
| `ModuleNotFoundError: autostars` при запуске из exe | собрано не из корня проекта или `autostars_bot.py` не рядом со spec; пересоберите `build.bat` |
| GUI: «процесс не запущен», в логе `No module named autostars.main` | GUI запущен вне папки проекта; `install.bat` правит это (мост подставляет `PYTHONPATH` и рабочую папку) |
| В консоли «кракозябры» | `.bat` делают `chcp 65001`; для своего ярлыка: `cmd /c chcp 65001 && …`; либо `$env:PYTHONIOENCODING="utf-8"` |
| Файлы `.bat` ругаются на «неизвестная команда» после правки из VS Code | перевод строк должен быть CRLF, кодировка — UTF-8 **без** BOM (иначе `@echo off` не исполняется) |
| `pip install flet` долго/падает | flet тянет Flutter-рантайм; ставьте ядро без GUI (`pip install -e .`) и запускайте CLI |
| Битый `ico.ico` | PyInstaller падает на иконке: уберите файл или соберите 32×32/48×48/256×256 в одном `.ico` |
| exe весит ~15-25 МБ и стартует 1-3 с | норма для one-file (распаковка в `%TEMP%`); для быстрого старта — `--onedir` |
| Два процесса, заказы «двойные» | цикл запущен и в GUI, и в консоли: оставьте один (база общая, `orders` защищает от дублей, но ответы покупателям могут задвоиться) |

## 8. Что нельзя терять при переносе

- `autostars.db` — история заказов и флаг паузы (статистика считается по ней);
- `.env` — ключи (в git не попадает, `chmod` на Windows не работает — просто не
  кладите папку проекта в публичные каталоги и облака);
- `.env.bak` — копия настроек до последней правки из GUI; удалите, если в ней
  старый ключ.

Перенос на другую машину: скопировать `dist\` + `.env` + `autostars.db`, запустить
`AutoStarsBot.exe --check`. Миграции схемы применятся сами.
