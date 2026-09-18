# 📓 Журнал изменений

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версия —
[SemVer](https://semver.org/lang/ru/). Дата релиза — дата коммита в `main`.

## [2.3.0] — 2026-09-18

Ревизия проекта по образцу гигиены референса: один поддерживаемый путь запуска,
безопасная конфигурация, тесты, сборка `.exe`, CI и честная документация.
Совместимость: `.env` и `autostars.db` из 2.2 подходят как есть; файловая схема
`db.json` осталась только в `legacy/`.

### Добавлено

- **Пакет как установленный**: `pyproject.toml` (метаданные, `dependencies`, extras
  `gui`/`dev`/`build`, консольные скрипты `autostars` и `autostars-gui`) — можно
  `pip install -e .` вместо ручногоlist'а зависимостей.
- **Настольный GUI поверх боевого движка** (`autostars/gui/`): вкладки «Главная»,
  «Статистика», «Заказы», «Настройки», «Логи», «Диагностика». Логика вынесена в
  flet-независимые `bridge.py` (subprocess-управление циклом, чтение SQLite/логов)
  и `settings.py` (редактор `.env` с валидацией и бэкапом) — обе покрыты тестами.
- **Пути приложения** (`autostars/paths.py`): `.env`, `config.json`, база и лог
  ищутся рядом с exe/корнем проекта, переопределение через `AUTOSTARS_HOME`,
  `AUTOSTARS_ENV`, `AUTOSTARS_CONFIG`, `--config`.
- **Защита секретов** (`autostars/security.py` + `mask_secret`/`redact`): ключи
  маскируются в логах, алертах и `--config-show`; консольный лог пишется в `stderr`,
  чтобы `--stats --json` можно было перенаправлять в файл.
- **CLI**: `--config-show`, `--json`, `--stats-hours`, `--timeline`, `--tasks`,
  `--retry-order`, `--balance`, `--pause`/`--resume`, `--deposit-info`, `--catalog`,
  `--test-order` (+`--test-qty`, `--dry-run`, `-y`), `--version`, `--config`.
- **TG-команды**: `/stop`, `/retry <id>`, `/calc`, `/status` с балансом и курсом.
- **Graceful shutdown** в `run_bot()`: SIGTERM/SIGINT, `/stop`, ожидание начатых
  выдач до `SHUTDOWN_TIMEOUT_SEC`, закрытие БД и HTTP-клиентов; ретраи логина на
  старте (`STARTUP_RETRY_DELAY`, `STARTUP_MAX_RETRIES`).
- **Параллельная выдача** с ограничением: семафор `MAX_CONCURRENT_ORDERS`.
- **SQLite**: `journal_mode=WAL`, `busy_timeout`, `foreign_keys`, создание
  родительской папки базы, индекс по статусам.
- **Конфигурация без падений**: `parse_errors` для битых чисел (было — `ValueError`
  и мёртвый процесс), приоритет «процесс > `.env` > `config.json` > код», поддержка
  `config.json` с секциями и любым регистром ключей, `.env` с инлайн-комментариями.
- **Установка на Windows**: `install.bat`, `start_gui.bat`, `run_bot.bat`,
  `build.bat` (UTF-8 + CRLF, `chcp 65001`), профиль `autostars.spec`, `ico.ico`.
- **Docker/systemd**: `Dockerfile` (non-root, healthcheck `--check`),
  `docker-compose.yml` (`./data`, `stop_grace_period`), `deploy/autostars.service`.
- **CI** (`ci/github-ci.yml`, включается копированием в `.github/workflows/ci.yml`):
  ruff + `compileall` + pytest на 3.10/3.11/3.12,
  смоук CLI, GUI-смоук (`tools/gui_smoke.py`), legacy-тесты (не блокирующие),
  `docker build`.
- **Тесты**: `tests/conftest.py` (`isolated_env` — ни один тест не трогает рабочие
  `.env`/БД), `test_config.py`, `test_config_drift.py` (расхождение
  `.env.example` ↔ `Config` ↔ полей GUI теперь падает), `test_security_paths.py`,
  `test_gui_bridge.py` (реальный subprocess: запуск, логи, остановка),
  `test_gui_settings.py`, `test_cli_smoke.py` (19 проверок CLI). Итого 154 теста ядра
  и интерфейса против 69 до правки.
- **Документация**: переписаны `README.md`, `FEATURES.md`, `INSTALL.md`; добавлены
  `CHANGELOG.md`, `CONTRIBUTING.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`,
  `docs/TESTING.md`, `docs/WINDOWS_BUILD.md`, `docs/REFERENCES.md`, `legacy/README.md`.
- `check_env.py` — диагностика Python/пакетов/прав/ключей/путей одной командой.
- `.env.example` дополнен реальными ключами движка (`FUNPAY_BASE_URL`,
  `GAMEAU_TIMEOUT`, `GAMEAU_MAX_RETRIES`, `DEFAULT_STARS_QUANTITY`,
  `TRON_ENERGY_FEE_RUB`, `COST_PER_STAR_USD`, `REVENUE_PER_STAR_RUB`,
  `USD_TO_RUB`, `MAX_CONCURRENT_ORDERS`, `SHUTDOWN_TIMEOUT_SEC`, `STARTUP_*`).

### Изменено

- `main.py`, `bot/`, `gui.py`, `config.example.json`, `photo.png` и тесты legacy
  перенесены в `legacy/` (с сохранением истории `git mv`); в корне больше нет
  второго и третьего движка.
- `requirements.txt` — только рантайм-минимум; для разработки и GUI — extras из
  `pyproject.toml` (`.[dev]`, `.[gui]`).
- Артефакты запуска больше не мешаются в репозиторий: `.env`, `autostars.db`, `autostars.log`
  создаются в папке приложения (для exe — рядом с exe) и исключены из git. Первый запуск
  создаёт `.env` из примера и завершается **ненулевым** кодом, если ключи не заполнены,
  — systemd и скрипты не примут это за успех.
- `--check` разделяет локальную диагностику и сетевые проверки: проблемы сети видны
  отдельной строкой `[ERR]`, а не общим «всё сломалось».
- GUI-настройки: 38 полей, значения по умолчанию совпадают с `.env.example`
  (раньше форма предлагала `STARTUP_MAX_RETRIES=10` там, где движок означает `0` =
  бесконечно, и `BALANCE_CHECK_INTERVAL_MIN=30` вместо 10 — «сохранил как есть»
  меняло поведение).
- Уровни логирования: `aiosqlite` больше не пишет DEBUG-спам в общий лог.

### Исправлено

- `Config.load()` не перечитывал `.env` после сохранения из GUI: значения из файла
  попадали в `os.environ` через `setdefault`, и GUI показывал старое. Теперь
  «файловые» значения отслеживаются (`_ENV_INJECTED`) и обновляются, а переменная,
  выставленная оператором/systemd, по-прежнему главнее файла.
- Алерт о зависшей задаче содержал мусор вместо текста (битый f-string) — теперь
  читаемое сообщение с Gameau ID и статусом.
- Вложенные f-strings с кавычками ломали сборку на Python 3.10/3.11.
- `--test-order` без подтверждения ничего не отправлял, но возвращал код 0 — теперь
  код 2, чтобы скрипт/CI заметили, что выдача не выполнялась.
- Чтение заказов в GUI падало на базе со старой схемой (нет колонки
  `gameau_order_id`) — запрос строится по фактическим колонкам.
- `redact()` не вычищал `api_key` внутри JSON и токены Telegram в URL — шаблоны
  исправлены, добавлены тесты.
- Сохранение настроек из GUI не создавало `.env.bak` «на всякий случай» и не теряло
  инлайн-комментарии: бэкап пишется только при реальном изменении.
- `legacy/tests` не собирались без `FunPayAPI` — теперь legacy ставится из
  `legacy/requirements.txt` (CI использует `--no-deps` из-за отсутствующей на PyPI
  версии `FunPayAPI==1.0.4.20`) и все 45 тестов проходят.

### Удалено

- `INSTALL_NET.md` — инструкция по C#/WPF-версии, не имеющая отношения к этому
  коду (ввела бы в заблуждение).

## [2.2.0]

- Чат-мониторинг `WAITING_USERNAME` (подхват `@username` из чата FunPay без спама).
- Пауза/резюм с флагом в БД, ретрай проваленных заказов, контроль баланса GAMEAU
  с порогом и алертом.
- Интерактивный TG-бот владельца (long polling `getUpdates`, разрешение по chat_id).
- Персистентные HTTP-соединения GAMEAU (пул `AsyncClient` вместо запроса с новым
  TLS-хендшейком).
- Защита от повторной обработки заказов по любому статусу в БД.

## [2.1.0]

- Трекинг выполнения задач: журнал `task_events`, детектор зависших,
  reconciliation незавершённых заказов.
- Статистика по окнам (1/2/3/4/6/24 ч, календарный день, всего), P&L, убыточные
  сделки, потерянная выручка, списания без выдачи.
- Telegram-алерты с прибылью по обоим курсам, `--stats-push`,
  `STATS_PUSH_INTERVAL_MIN`.

## [2.0.0]

- Асинхронный движок `autostars/` (httpx + aiosqlite + BeautifulSoup) вместо
  синхронного `bot/`.
- `Idempotency-Key` (UUID v5) и `maxCharge` для защиты от двойных и избыточных
  списаний; фактическая себестоимость по `chargedAmount`.
- SQLite-модель `orders` / `task_events` / `idempotency_keys` / `settings`,
  миграции схемы.
- Long polling FunPay `runner/` + автоматическое обновление CSRF при 403.

## [1.x] — AutoStars-New (legacy)

- Первая версия: `main.py` + `bot/`, конфигурация в `db.json`, синхронные
  запросы, однопоточная обработка. Поддержки не имеет, исходники — в `legacy/`.
