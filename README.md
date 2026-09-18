# 🌟 AutoStars — автовыдача Telegram Stars на FunPay через GAMEAU

Автоматизированная выдача Telegram Stars покупателям FunPay. Покупатель платит
рубли на FunPay, себестоимость — USDT TRC-20 на балансе [gameau.us](https://gameau.us)
(один из двух маршрутов завода), сама выдача идёт через GAMEAU REST API v1
([документация](https://gameau.us/api-docs.html)).

Поддерживаемый движок — пакет [`autostars/`](autostars) (async: `httpx` + `aiosqlite`).
Рядом лежит **тот же** функционал в трёх оболочках: CLI (`autostars_bot.py`),
настольный GUI на flet (`autostars_gui.py`) и Docker/systemd — все они управляют
одним процессом и одной базой, «своей» логики выдачи в GUI нет.

Исходный релиз AutoStars-New (`main.py` + `bot/` + `gui.py`) изолирован в
[`legacy/`](legacy) и не поддерживается — [почему](legacy/README.md).

## ✨ Что умеет

- 🤖 **Автовыдача** — опрос оплаченных заказов FunPay (HTML + long polling `runner/`),
  извлечение `@username` и количества звёзд, покупка пакета через GAMEAU, ответ покупателю
- 🛡️ **Денежная защита** — `Idempotency-Key` (UUID v5 от номера заказа), лимит
  `maxCharge` от цены пакета из каталога (+`MAX_CHARGE_MARGIN_PCT`), фактическая
  себестоимость по `chargedAmount`, ретраи только на сетевые 429/5xx
- 🗂 **Трекинг задач** — журнал жизненного цикла заказа, детектор «зависших»,
  авто-согласование (reconciliation) незавершённых заказов с GAMEAU
- 📊 **Статистика и P&L** — окна 1/2/3/4/6/24 ч, календарный день и «всего»: заказы,
  звёзды, выручка ₽, затраты USDT/₽, прибыль, маржа, убыточные сделки, провалы
- 📱 **Telegram** — алерт по каждой сделке (прибыль по обоим курсам), критические
  события, авто-отправка статистики и **управление с телефона**: `/status /stats
  /report /tasks /balance /pause /resume /retry <id> /calc /stop`
- 💬 **Чат-мониторинг** — если покупатель написал `@username` в чат FunPay, выдача
  стартует сама, без повторного запроса
- 🖥 **GUI (flet)** — запуск/остановка цикла, статус, статистика, заказы, хвост логов,
  редактор настроек с проверкой значений; секреты в форме не показываются
- 📦 **Установка в один клик** — `install.bat` (Windows) / `pip install -e .`, сборка
  `.exe` через `build.bat`, самодиагностика `check_env.py`
- ⚙️ **Эксплуатация** — пауза/резюм, ретрай заказа, `--check`, `--config-show`,
  `--stats --json` (для дашбордов), graceful shutdown, `.env` / `config.json` /
  переменные окружения на выбор

## 📁 Структура

```
autostars/                    # поддерживаемый движок (async)
├── main.py                   # CLI + главный цикл автовыдачи (запуск, shutdown, /stop)
├── config.py                 # конфигурация: .env + config.json + env, диагностика ошибок
├── paths.py                  # пути приложения (.env/БД/лог рядом с exe или проектом)
├── security.py               # маскирование и redact секретов в логах
├── clients/
│   ├── funpay.py             # FunPay: csrf, long polling runner/, чаты, ответы
│   └── gameau.py             # GAMEAU API v1: каталог, telegramStars, статусы, баланс
├── database/
│   ├── models.py             # схема SQLite (orders, task_events, idempotency, settings)
│   └── db_manager.py         # CRUD, WAL/busy_timeout, миграции, агрегаты статистики
├── services/
│   ├── parser.py             # @username / t.me/... / количество звёзд из текста
│   ├── order_processor.py    # пайплайн: парсинг → GAMEAU → ответ → алерт (ретраи, семафор)
│   ├── statistics.py         # окна статистики, убытки, отчёты, JSON
│   ├── task_tracker.py       # события задач, «зависшие», reconciliation
│   └── bot_control.py        # пауза/резюм, флаг в БД, состояние цикла
├── notifier/
│   ├── tg_alert.py           # Telegram-алерты + калькуляция прибыли
│   └── tg_commands.py        # интерактивный бот владельца (long polling)
└── gui/                      # настольный интерфейс (flet), thin-контроллер над движком
    ├── bridge.py             # запуск/остановка процесса, чтение БД/логов (без flet!)
    ├── settings.py           # редактор .env: валидация, бэкап, сохранение комментариев
    └── app.py                # вкладки: Главная · Статистика · Заказы · Настройки · Логи · Диагностика

autostars_bot.py              # точка входа CLI (для pythonw/exe)
autostars_gui.py              # точка входа GUI
check_env.py                  # самодиагностика окружения (python, пакеты, ключи, пути)
install.bat · start_gui.bat · run_bot.bat · build.bat   # Windows: установка, запуск, сборка exe
autostars.spec                # профиль PyInstaller для CLI (GUI собирается через `flet pack`)
config.example.json · .env.example                      # примеры конфигурации
Dockerfile · docker-compose.yml · deploy/autostars.service
tests/                        # pytest движка, CLI, моста GUI и настроек
tools/gui_smoke.py            # headless-проверка, что интерфейс собирается
legacy/                       # исходный AutoStars-New: main.py, bot/, gui.py, свои тесты
ci/github-ci.yml              # CI (копируется в .github/workflows/ci.yml — см. ci/README.md)
docs/                         # ARCHITECTURE · SECURITY · TESTING · WINDOWS_BUILD · REFERENCES
```

## 🚀 Быстрый старт

### Windows (без консоли и без Python-ритуалов)

```bat
git clone https://github.com/MFcrychelt/funpay.git && cd funpay
install.bat            :: создаёт .venv, ставит пакеты, копирует .env.example → .env, показывает check_env
notepad .env           :: FUNPAY_GOLDEN_KEY, GAMEAU_API_KEY (TELEGRAM_* — по желанию)
start_gui.bat          :: графическая оболочка: «▶ Запустить цикл»
```

Нужен только запуск в консоли? `run_bot.bat --check`, затем `run_bot.bat`.
Хотите `.exe` без Python на машине — `build.bat`, папка `dist\` ([подробно](docs/WINDOWS_BUILD.md)).

### Linux / macOS / venv

```bash
git clone https://github.com/MFcrychelt/funpay.git && cd funpay
python3 -m venv .venv && . .venv/bin/activate
pip install -e .              # ядро;  с интерфейсом: pip install -e ".[gui]"

cp .env.example .env          # или запустите что угодно — .env создастся сам
nano .env

python check_env.py           # окружение, версии пакетов, права, наличие ключей
autostars --check             # диагностика: конфиг → SQLite → FunPay → GAMEAU → курсы
autostars --test-order your_username --dry-run   # смета теста (50⭐), ничего не покупает
autostars --test-order your_username --test-qty 20 -y   # реальная проверка на своём аккаунте
autostars                     # основной цикл автовыдачи
```

`autostars` — консольный скрипт из `pyproject.toml`; он равен
`python -m autostars.main`. Интерфейс: `autostars-gui` или `python autostars_gui.py`.

### Docker / systemd

```bash
cp .env.example .env && nano .env
docker compose up -d --build && docker compose logs -f
```

Для установки на VPS без Docker — [deploy/autostars.service](deploy/autostars.service)
(`AUTOSTARS_HOME=/var/lib/autostars`, `EnvironmentFile=.env`, `TimeoutStopSec=90`,
`Restart=always`). В обоих случаях движок использует те же файлы и ту же базу — можно
переезжать с Docker на systemd без миграции.

## 📊 Команды CLI

Все команды работают и в исходниках, и в exe (`AutoStarsBot.exe --check`).
Машинный вывод — флагом `--json` (для `--stats`, `--report`, `--tasks`, `--balance`).

| Команда | Назначение |
|---|---|
| `autostars` | основной цикл: опрос FunPay + long polling + чат-мониторинг + reconcile |
| `--once` | однократный проход по очереди (cron) |
| `--check` | диагностика: конфиг, SQLite, FunPay, GAMEAU, каталог, курсы, Telegram |
| `--config-show` | применённая конфигурация и источники (секреты замаскированы) |
| `--catalog` | каталог GAMEAU с себестоимостью по обоим курсам |
| `--calc 1370.40 [1000] [USDT]` | калькулятор прибыли: Вариант 1 против Варианта 2 |
| `--deposit-info` | как завести USDT TRC-20 (оба маршрута, комиссии, риски) |
| `--stats` / `--report` | статистика по окнам / полный отчёт (P&L, убытки, топ, провалы) |
| `--stats-hours 1,6,24` | свои часовые окна для `--stats` |
| `--tasks` / `--timeline <ID>` | открытые и зависшие задачи / журнал одной задачи |
| `--stats-push` | отправить текущую статистику в Telegram |
| `--balance` | баланс GAMEAU + «на сколько заказов 1000⭐ хватит» |
| `--pause` / `--resume` | пауза приёма новых заказов (флаг в БД, переживает рестарт) |
| `--retry-order <ID>` | повторить проваленный заказ (идемпотентно, без двойной покупки) |
| `--test-order <username>` `--test-qty 50` `--dry-run` `-y` | проверка выдачи на своём аккаунте |
| `--config PATH` | явный `config.json` вместо `.env` |
| `-v` / `--version` | отладочный уровень / версия |

Коды возврата: `0` — ок, `1` — есть проблемы (нет ключей, сеть, заказ не найден),
`2` — нужна явная отмашка (например, `--test-order` без `--yes`).

### Управление с телефона

Живой цикл слушает команды **только владельца** (`TELEGRAM_CHAT_ID`), long polling —
вебхук и открытые порты не нужны: `/help` `/status` `/stop` `/pause` `/resume`
`/stats` `/report` `/tasks` `/balance` `/retry 123456` `/calc 1370.4 1000`.

## 💰 Финансовая модель (USDT TRC-20 → gameau.us)

| | Вариант 1 | Вариант 2 (рекомендуется) |
|---|---|---|
| Маршрут | Анонимный обмен / P2P (Telegram Wallet, BestChange, криптоматы) | Беларусь, Whitebird — обмен по карте, без P2P |
| Курс | ~110.00 ₽ / USDT | ~87.63 ₽ / USDT |
| Себестоимость 1000⭐ (9.10 USDT) | ~1001.00 ₽ | ~797.43 ₽ |
| Прибыль при продаже за 1370.40 ₽ | +369.40 ₽ (маржа 27.0 %) | +572.97 ₽ (маржа 41.8 %) |

Активный курс — `EXCHANGE_VARIANT=1|2`. По каждой сделке Telegram-алерт считает
прибыль **по обоим** вариантам, статистика — по активному.

```bash
autostars --calc 1370.4 1000
# • Вариант 1 (анонимный обмен/P2P, 110.00 ₽/USDT)  себестоимость 1001.00 ₽ | прибыль +369.40 ₽ | маржа 27.0%
# • Вариант 2 (Whitebird (РБ), 87.63 ₽/USDT) 💰     себестоимость  797.43 ₽ | прибыль +572.97 ₽ | маржа 41.8%
```

## ⚙️ Конфигурация

Источники, приоритет сверху вниз: **переменные процесса** → **`.env`** →
**`config.json`** → значения по умолчанию в коде. Файлы ищутся рядом с exe /
корнем проекта; переопределяется `AUTOSTARS_HOME`, `.env` — `AUTOSTARS_ENV`,
`config.json` — `--config` / `AUTOSTARS_CONFIG`. Пример JSON —
[config.example.json](config.example.json) (ключи те же, что в `.env`).

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `FUNPAY_GOLDEN_KEY` | — | ключ продавца FunPay (**обязательно**) |
| `GAMEAU_API_KEY` | — | API-ключ gameau.us (**обязательно**) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | алерты и команды владельца |
| `EXCHANGE_VARIANT` | `2` | активный курс завода: 1 = P2P 110 ₽, 2 = Whitebird 87.63 ₽ |
| `RATE_VARIANT_1` / `RATE_VARIANT_2` | `110.00` / `87.63` | курсы ₽/USDT |
| `USDT_PER_1000_STARS` | `9.10` | оценка себестоимости, когда каталог недоступен |
| `DEFAULT_MAX_CHARGE_USDT` | `9.50` | нижняя граница лимита списания на заказ |
| `MAX_CHARGE_MARGIN_PCT` | `5` | запас к цене пакета из каталога в `maxCharge` |
| `TRON_ENERGY_FEE_RUB` | `0` | комиссия завода на сделку (влияет на прибыль) |
| `HIDE_SENDER` | `false` | скрывать отправителя звёзд |
| `POLL_INTERVAL` | `5.0` | опрос очереди, сек |
| `DEFAULT_STARS_QUANTITY` | `1000` | звёзды, если количество не распознано |
| `CHAT_MONITOR_INTERVAL_SEC` | `15` | подхват `@username` из чата FunPay |
| `STUCK_TASK_MINUTES` | `20` | порог «зависшей» задачи |
| `RECONCILIATION_INTERVAL_SEC` | `60` | согласование незавершённых заказов с GAMEAU |
| `MAX_ORDER_RETRIES` | `2` | повторы при сетевых сбоях GAMEAU |
| `WAIT_COMPLETION_TIMEOUT` | `120` | ожидание финального статуса GAMEAU |
| `MAX_CONCURRENT_ORDERS` | `4` | сколько заказов вести параллельно (семафор) |
| `SHUTDOWN_TIMEOUT_SEC` | `30` | сколько ждать начатые выдачи при остановке |
| `STARTUP_RETRY_DELAY` / `STARTUP_MAX_RETRIES` | `15` / `0` | повторы логина при старте (`0` = вечно) |
| `LOW_BALANCE_THRESHOLD_USDT` / `BALANCE_CHECK_INTERVAL_MIN` | `50.0` / `10` | контроль баланса GAMEAU и алерт |
| `STATS_PUSH_INTERVAL_MIN` | `0` | авто-статистика в Telegram (`0` — выкл) |
| `DB_PATH` / `LOG_FILE` | `autostars.db` / `autostars.log` | где лежать базе и логу |
| `LOG_MAX_BYTES` × `LOG_BACKUP_COUNT` | `5000000` × `5` | ротация лога |
| `GAMEAU_BASE_URL` / `GAMEAU_TIMEOUT` / `GAMEAU_MAX_RETRIES` | `https://gameau.us/api/v1` / `20` / `3` | сеть до GAMEAU |
| `FUNPAY_BASE_URL` / `FUNPAY_USER_AGENT` | `https://funpay.com` / Chrome UA | доступ к FunPay (403 лечается UA) |

Полный список с пояснениями — [.env.example](.env.example); он же источник
подсказок в GUI, и расхождение «пример ↔ код ↔ форма» ловит тест
[tests/test_config_drift.py](tests/test_config_drift.py).

Некорректное число (`POLL_INTERVAL=abc`) больше не роняет процесс: значение
заменяется разумным по умолчанию, а ошибка видна в `--check`,
`--config-show` и в логе (`⚠️ замечания`).

## 🧪 Проверка и тесты

```bash
pip install -e ".[dev]"
python -m pytest -q                      # 154 теста: движок, CLI, GUI-мост, настройки
python -m pytest legacy/tests -q          # 45 тестов legacy-реализации
python tools/gui_smoke.py                 # собирается ли интерфейс (без запуска окна)
python -m ruff check autostars tests      # линтер (конфиг в pyproject.toml)
```

CI ([ci/github-ci.yml](ci/github-ci.yml)) прогоняет это на Python 3.10/3.11/3.12,
отдельно — GUI-смоук, legacy, проверка ссылок в документации и `docker build`. Файл
нужно один раз скопировать в `.github/workflows/ci.yml` — [как и почему](ci/README.md).
Подробности — [docs/TESTING.md](docs/TESTING.md).

## 🔒 Безопасность и риски

Ключи живут только в `.env` (в `.gitignore`); в логах и в GUI они маскируются
(`abcd…********…wxyz`, `redact()` чистит строки от `golden_key`/`api_key`/токенов
Telegram), в `--config-show` выводятся только замаскированные значения.
`maxCharge` не даёт списать больше согласованного, а `Idempotency-Key` — купить
звёзды дважды при ретрае. Ключ GAMEAU уходит только на `gameau.us`, входящих портов у системы нет.

Автоматизация FunPay — зона риска самого продавца (правила площадки), покупка
звёзд — реальные деньги: начните с `--test-order --dry-run`, затем 20–50⭐ на
свой аккаунт. Подробно — [docs/SECURITY.md](docs/SECURITY.md).

## 📚 Документация

| Документ | О чём |
|---|---|
| [INSTALL.md](INSTALL.md) | установка на Windows/Linux/Docker, где взять ключи, первый запуск, «частые проблемы» |
| [FEATURES.md](FEATURES.md) | что умеет и какими параметрами управляется (по модулям) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | слои, поток данных заказа, схема БД, shutdown, инварианты денег |
| [docs/SECURITY.md](docs/SECURITY.md) | секреты, права на файлы, маскирование в логах, риски площадки, чек-лист |
| [docs/TESTING.md](docs/TESTING.md) | как запускать тесты, карта тестов, правила (без сети, без денег) |
| [docs/WINDOWS_BUILD.md](docs/WINDOWS_BUILD.md) | `install.bat`, `build.bat`, `.exe`, автозапуск, антивирусы |
| [docs/REFERENCES.md](docs/REFERENCES.md) | референс-проект, версии зависимостей, используемые концы API |
| [ci/README.md](ci/README.md) | что проверяет CI и как включить workflow одним копированием |
| [CHANGELOG.md](CHANGELOG.md) · [CONTRIBUTING.md](CONTRIBUTING.md) | история версий; как вносить изменения |
| [legacy/README.md](legacy/README.md) | почему старый код изолирован и как перенести `db.json` |

## 📄 Лицензия

MIT — см. [LICENSE](LICENSE). Проект независимый: FunPay и GAMEAU — сторонние
сервисы, их правила и тарифы меняются без предупреждения.
