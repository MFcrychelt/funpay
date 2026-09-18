# 📓 Журнал изменений

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версия —
[SemVer](https://semver.org/lang/ru/). Дата релиза — дата коммита в `main`.

## [2.4.0] — 2026-09-18

Функционал, а не только гигиена: движок научился решать «не покупать», говорить с
покупателем настраиваемыми текстами, отдавать историю наружу и показывать состояние
мониторингу. Всё — с тестами (247) и без новых зависимостей.

### Добавлено

- **Политика выдачи (`autostars/services/policy.py`)** — проверки считаются **до**
  запроса в GAMEAU (списанные USDT обратно не вернуть), итог — `ignore` / `alert` /
  `hold`:
  `MAX_ORDER_REVENUE_RUB` (крупная сделка), `MIN_MARGIN_PCT` (маржа по активному
  курсу), `DAILY_SPEND_LIMIT_USDT` (суточный бюджет закупки), `DUPLICATE_WINDOW_MIN` +
  `DUPLICATE_ACTION` (тот же ник и количество в окне), `BLACKLIST_ENABLED`
  (стоп-лист `buyer_flags`).
- **Статус `HOLD_MANUAL`** — задержанный заказ: не провал (статистика не врёт) и не
  «в работе» (reconciliation не поднимает «зависшая задача» каждые `STUCK_TASK_MINUTES`);
 входит в `--export --failed`, виден отдельной строкой «⛔ задержано N» в `--stats`.
- **Ручные операции**: `--held`, `--release <ID> -y` (выдать вопреки политике, через
  тот же пайплайн и с тем же `Idempotency-Key`), `--cancel <ID> -y [--note]`,
  `--order @nick [звёзды] [--dry-run] [-y]` — выдача вне сделки FunPay,
  `--limits [--json]`, `--customers [--days N]`,
  `--blacklist list|add @nick [причина]|remove @nick`.
- **Шаблоны ответов покупателю** — `REPLY_NEED_USERNAME`, `REPLY_DELIVERED`,
  `REPLY_HOLD`, `REPLY_ERROR` (пусто = не отправляем). Подстановки `{username}`,
  `{quantity}`, `{quantity_spaces}`, `{order_id}`, `{price_rub}`, `{reason}`;
  неизвестный ключ остаётся в тексте и ничего не ломает. Значения по умолчанию
  совпадают с текстами, которые движок писал раньше, поэтому переписка не меняется.
- **Выгрузка истории** — `--export файл.csv|json` с `--since 7d|24h|2026-09-01..2026-09-07`,
  `--failed`, `--buyer @nick`, `--export-status`, `--with-events`, `--export-limit`;
  CSV — `utf-8-sig` + CRLF (открывается в Excel), запись атомарная, SQL — в
  `DBManager.select_orders`, форматирование — в `services/export.py`.
- **Метрики** (`autostars/metrics.py`) — `GET /metrics` (Prometheus-текст), `/healthz`
  (503 при проблемах конфигурации), `/status` (JSON): свой лёгкий HTTP-сервер на
  `asyncio`, зависимости не добавлены; снимок пересчитывается раз в 15 с, scrape
  читает кэш. По умолчанию `METRICS_ENABLED=false` и `METRICS_HOST=127.0.0.1`;
  доступ извне — только через reverse-proxy с `METRICS_TOKEN`. CLI: `--metrics`.
- **«Тихие часы» (`MUTE_HOURS=23-07`)** — некритичные уведомления (успешная сделка,
  плановая статистика) в окне не отправляются; ошибки, низкий баланс, задержки и
  остановка цикла проходят всегда.
- **Telegram-команды**: `/limits`, `/held`, `/release <id>`, `/blacklist [add|remove
  @nick]`, `/customers [дней]` — те же обработчики, что у CLI (общие сервисы).
- **GUI**: вкладка «Выдача» (расход с полуночи против лимита, список задержанных с
  кнопками «отпустить»/«отклонить» — обе по второму клику, стоп-лист, выгрузка CSV),
  53 поля настроек (было 38): новая секция «Политика выдачи» и «Ответы покупателю»
  с многострочными полями; редактор `.env` экранирует переносы шаблонов и не даёт
  неизвестный `{плейсхолдер}`.
- **БД**: таблица `buyer_flags` (стоп-лист) + методы `add/remove/list/is_buyer_flagged`,
  `sum_cost_usdt_since`, `find_recent_order_by_username`, `buyer_stats`, `select_orders`;
  в оконной агрегат — счётчик `held`.
- **Тесты**: `tests/test_risk.py` (35), `tests/test_templates.py` (19),
  `tests/test_export_metrics.py` (25), +10 к CLI-смоуку → **243** теста.

### Изменено

- `.env.example` / `config.example.json` описывают все новые ключи; расхождение
  «пример ↔ `Config` ↔ поля GUI» по-прежнему ловит `tests/test_config_drift.py`.
- Ответ покупателю при успехе и при провале выдачи больше не зашит в код — он идёт из
  шаблона; при пустом шаблоне сообщение не отправляется (и в логе видно почему).
- `RETRYABLE_STATUSES` теперь включает `HOLD_MANUAL`, `parse_env_text` понимает
  многострочные значения в двойных кавычках (`\n`) и не режет `#` внутри них.
- `--help`, `README.md`, `FEATURES.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`,
  `docs/TESTING.md` описывают новые команды, статусы и границу «метрики — порт на
  localhost».

### Совместимость

- `.env`, `config.json` и `autostars.db` из 2.3 подходят как есть: новые колонки и
  таблица `buyer_flags` создаются миграцией при первом запуске, все новые проверки
  по умолчанию выключены (`0`/`false`), поведение пайплайна без политики идентично 2.3
  (это закреплено тестом).
- `HOLD_MANUAL` — новый статус: если у вас были внешние отчёты по статусам, добавьте
  его в «не провалы».

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
  `test_gui_settings.py`, `test_cli_smoke.py` (19 проверок CLI). Итого 247 тестов ядра
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
