# 🚀 Установка AutoStars

Три сценария: **Windows «как приложение»**, **Linux/VPS (systemd)**, **Docker**.
Везде один и тот же движок, один `.env` и одна SQLite-база — разница только в том,
как процесс запускается.

Проверьте окружение в любой момент: `python check_env.py` (или `install.bat`, он
вызывает это сам).

---

## 1. Windows

### 1.1. Установка «в один клик»

Нужен Python 3.10+ (установщик с python.org, галочка **Add python.exe to PATH**).

```bat
git clone https://github.com/MFcrychelt/funpay.git
cd funpay
install.bat
```

`install.bat` найдёт Python (`py -3` или `python`), проверит версию, создаст
`.venv`, поставит `pip install -e ".[gui]"`, скопирует `.env.example` → `.env` и
покажет результат `check_env.py`.

Дальше:

```bat
notepad .env        :: FUNPAY_GOLDEN_KEY, GAMEAU_API_KEY, TELEGRAM_* (опционально)
start_gui.bat       :: интерфейс: «▶ Запустить цикл»
run_bot.bat --check :: то же самое в консоли
```

`.bat`-файлы работают без активации venv (сами находят `.venv\Scripts\python.exe`)
и переключают кодовую страницу в UTF-8, чтобы русский вывод не превращался в «кракозябры».

### 1.2. Установка вручную (если хочется понимать, что происходит)

```bat
cd funpay
py -3 -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -e ".[gui]"
copy .env.example .env
.venv\Scripts\python check_env.py
.venv\Scripts\python autostars_bot.py --check
```

Только движок (без GUI) — `pip install -e .`.

### 1.3. EXE без Python на машине

```bat
build.bat
```

- `dist\AutoStarsBot.exe` — CLI (PyInstaller, один файл, консоль);
- `dist\AutoStars.exe` — GUI (его собирает `flet pack`, потому что окну нужен
  Flutter-рантайм, который в обычный spec не кладётся; `flet pack` по умолчанию делает
  one-file, с `--onedir` будет папка `dist\AutoStars\`).

Правила раскладки: кладите exe **в папку проекта целиком** (рядом должны лежать
`.env`, `autostars.db`, `autostars.log`) или задайте `AUTOSTARS_HOME`. Файлы
ищутся рядом с exe именно для случая «запуск двойным кликом из проводника», когда
рабочая папка — не та, где сам exe. Подробно, включая антивирусы и иконку:
[docs/WINDOWS_BUILD.md](docs/WINDOWS_BUILD.md).

---

## 2. Linux / VPS + systemd

```bash
sudo apt install -y python3-venv python3-pip git
git clone https://github.com/MFcrychelt/funpay.git /opt/autostars
cd /opt/autostars
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .            # GUI не нужен на сервере

sudo useradd -r -m -d /var/lib/autostars autostars
sudo cp .env.example /var/lib/autostars/.env
sudo nano /var/lib/autostars/.env     # ключи
sudo chown -R autostars:autostars /var/lib/autostars
sudo chmod 600 /var/lib/autostars/.env

sudo nano /etc/systemd/system/autostars.service   # см. deploy/autostars.service
sudo systemctl daemon-reload
sudo systemctl enable --now autostars
journalctl -u autostars -f
```

Юнит задаёт `AUTOSTARS_HOME=/var/lib/autostars` и `EnvironmentFile=` — база, лог и
`.env` лежат вhome сервиса, а не в репозитории. `TimeoutStopSec=90` покрывает
graceful shutdown (`SHUTDOWN_TIMEOUT_SEC=30` + запас).

Проверка без демона:

```bash
sudo -u autostars env AUTOSTARS_HOME=/var/lib/autostars /opt/autostars/.venv/bin/autostars --check
```

---

## 3. Docker

```bash
git clone https://github.com/MFcrychelt/funpay.git && cd funpay
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

Что внутри: non-root, `AUTOSTARS_HOME=/app/data` (том `./data`), healthcheck
выполняет `--check`, `init: true` + `stop_grace_period: 60s` — чтобы остановка
контейнера не обрывала покупку звёзд на середине. База и лог живут в
`./data/autostars.db` и `./data/autostars.log`.

---

## 4. Где взять ключи

| Ключ | Как получить |
|---|---|
| `FUNPAY_GOLDEN_KEY` | Войти в аккаунт **продавца** на funpay.com → DevTools (F12) → Application/Cookies → `golden_key`. Не `auth` и не с аккаунта покупателя |
| `GAMEAU_API_KEY` | gameau.us → профиль → «API для интеграций» → выпустить ключ. Показывается один раз |
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` |
| `TELEGRAM_CHAT_ID` | @userinfobot (или `getUpdates` своего бота) — числовой ID **вашего** аккаунта |

Без `TELEGRAM_*` всё работает, просто нет алертов и управления с телефона
(об этом скажет `--check`).

---

## 5. Первый запуск: порядок действий

```bash
autostars --check          # конфиг, SQLite, FunPay, GAMEAU, каталог, курсы, Telegram
autostars --catalog        # видит ли каталог и сколько стоит пакет (иначе оценка 9.10 USDT/1000⭐)
autostars --balance        # баланс GAMEAU и «на сколько заказов хватит»
autostars --calc 1370.4 1000   # экономика сделки, ничего не трогает
autostars --test-order ваш_username --dry-run    # смета реальной тестовой покупки
autostars --test-order ваш_username --test-qty 20 -y   # 20⭐ на свой аккаунт: проверка всех звеньев
autostars                  # основной цикл
```

`--test-order` без `--yes`/`--dry-run` **не** запускается: программа покупает
звёзды за реальные USDT и просит явную отмашку (код возврата 2).

Дальше — выложить лот на FunPay и проверить, что заказ проходит цикл
`RECEIVED → … → COMPLETED` (`autostars --timeline <ID>` заказа виден и в GUI).

---

## 6. Конфигурация

Приоритет: **переменные процесса → `.env` → `config.json` → код**.

| Что | Как задать |
|---|---|
| Обычная установка | `.env` в папке проекта (создаётся сам из `.env.example`) |
| EXE | `.env` рядом с exe |
| systemd/Docker | `EnvironmentFile=` / `env_file=` + `AUTOSTARS_HOME=` |
| Ansible/CI, «один файл — много ключей» | `config.json` (пример: [`config.example.json`](config.example.json)) или `--config /путь/config.json` |

Все настраиваемые ключи с пояснениями — [.env.example](.env.example); те же поля
есть в GUI («Настройки»). JSON принимает и плоский вид (`"POLL_INTERVAL": 5`), и
секции (`{"funpay": {...}}`), ключи — в любом регистре.

---

## 7. Обновление

```bash
cd funpay && git pull
.venv/bin/pip install -e .        # Windows: install.bat повторно — он обновит venv
.venv/bin/python check_env.py
.venv/bin/autostars --config-show # убедиться, что новые ключи прочитались
```

Структура базы меняется совместимо (миграции добавят колонки); `autostars.db`
удалять не нужно. Перед обновлением полезно `autostars --pause` + дождаться
окончания текущих выдач.

---

## 8. Частые проблемы

| Симптом | Что делать |
|---|---|
| `Отсутствуют обязательные параметры конфигурации: …` | В `.env` остались значения-заглушки (`paste_…`) — впишите реальные ключи. Проверьте, что смотрите тот `.env`, который показывает `--config-show` в строке «источники» |
| FunPay отвечает `403` | Устаревший `golden_key` (войти заново), чужой аккаунт (нужен продавец), или блокировка UA → раскомментируйте/поменяйте `FUNPAY_USER_AGENT`. иногда помогает смена IP; с мобильного интернета FunPay капризничает |
| `TLS/SSL connection has been closed` | Прокси/фильтр/ DPI режет HTTPS: проверьте `curl -I https://gameau.us`, временно выключите антивирусный web-щит, на VPS — что у хостера нет блокировки 443 на этот домен |
| `⚠️ Каталог недоступен` | Не страшно: себестоимость считается по `USDT_PER_1000_STARS`; но `maxCharge` тогда = `DEFAULT_MAX_CHARGE_USDT`. Починить сеть до GAMEAU |
| `POLL_INTERVAL=…` в «замечаниях» | В `.env` нечисловое значение (часто после правки «по верхней строке»): движок подставил разумное по умолчанию — исправьте файл |
| Заказы не подхватываются | Проверьте `--tasks`/`--pause` (снятие паузы: `--resume`), что лот требует ответа в чате (именно такие заказы забираются), что второй экземпляр бота не «съел» очередь |
| `database is locked` | Второй процесс с той же базой (например, цикл в консоли + цикл в GUI). Оставьте один; WAL уже включён |
| GUI: «процесс не запущен», в логах `ModuleNotFoundError: autostars.main` | GUI запущен не из папки проекта или без venv: `install.bat`, либо `python autostars_gui.py` из корня. Мост сам подставляет `PYTHONPATH`, но пакет должен быть виден |
| GUI не открывается / `flet` не найден | `pip install "flet>=0.86.1,<1"`; flet 1.x — другой API, на нём интерфейс не собирается |
| Windows: «Система Windows защитила ваш компьютер» | SmartScreen → «Подробнее» → «Выполнить в любом случае». Для постоянных сборок — подпись кода; антивирусы реагируют на PyInstaller one-file |
| В консоли «кракозябры» | `.bat` ставят `chcp 65001`; вручную: `chcp 65001` или `$env:PYTHONIOENCODING="utf-8"` |
| После остановки остались «в работе» заказы | Это нормально: при следующем старте reconciliation сверит их с GAMEAU и закроет сам |

Полезное на крайний случай:

```bash
autostars --config-show     # что применилось и откуда
autostars --check           # где именно отваливается (FunPay / GAMEAU / БД / TG)
python check_env.py         # версии пакетов, права, пути
autostars -v --once         # один проход с отладочным логом
```

Логи и база — в `LOG_FILE` / `DB_PATH` (по умолчанию рядом с проектом/exe);
`.env.bak` — автоматическая копия настроек перед сохранением из GUI.
