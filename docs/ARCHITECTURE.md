# 🏗 Архитектура AutoStars

Документ для тех, кто собирается менять код. Описаны слои, поток данных, инварианты
(нарушение которых стоит денег) и границы модели.

## 1. Слои

```
                    ┌──────────── CLI ────────────┐  ┌───────── GUI (flet) ─────────┐
пользователь ──────▶│ autostars/main.py (argparse)│  │ gui/app.py  — только вид       │
                    │ autostars_bot.py / exe      │  │ gui/bridge.py — процесс и БД   │
                    └──────────────┬──────────────┘  │ gui/settings.py — редактор .env│
                                   │                  └───────────────┬────────────────┘
                                   ▼                                  │ subprocess/SQLite/.env
                    ┌───────────────────────────────────────────────────┐
                    │            движок (единый для всех оболочек)        │
                    │  services/order_processor · policy · export ·       │
                    │  bot_control · task_tracker · statistics · parser   │
                    ├─────────────────────────────────────────────────────┤
                    │  notifier/tg_alert · tg_commands   metrics (опц.)   │
                    └───────────────┬───────────────────┬─────────────────┘
                                    ▼                   ▼
                    ┌───────────────────────┐ ┌───────────────────────────┐
                    │ clients/funpay.py     │ │ clients/gameau.py         │
                    │ clients/…telegram     │ │ (httpx, пул соединений)   │
                    └───────────┬───────────┘ └────────────┬──────────────┘
                                ▼                          ▼
                    ┌───────────────────────────────────────────────────────┐
                    │ database/db_manager.py — SQLite (WAL), единственное    │
                    │ хранилище состояния: заказы, журнал задач, флаги       │
                    └───────────────────────────────────────────────────────┘
```

| Модуль | Ответственность | Чего здесь быть не должно |
|---|---|---|
| `autostars/main.py` | разбор аргументов, цикл, под-команды, shutdown | бизнес-решений по выдаче (они в `services/`) |
| `autostars/config.py` | чтение/валидация/приоритеты конфигурации, `issues()` | обращений в сеть и в БД |
| `autostars/paths.py` | где лежат `.env`, `config.json`, БД, лог (в т. ч. в exe) | логики конфигурации |
| `autostars/security.py` | маскирование и redact секретов | — |
| `clients/funpay.py` | HTML/CSRF/long polling/чат FunPay | знать про GAMEAU и про статистику |
| `clients/gameau.py` | каталог, покупка, статусы, баланс, `Idempotency-Key`, `maxCharge` | писать в БД |
| `services/order_processor.py` | пайплайн одного заказа, ретраи, ответ покупателю | заводить свой цикл/таймеры |
| `services/policy.py` | решения «покупать ли»: лимиты, дубли, стоп-лист; шаблоны ответов; текст алерта владельцу | ходиться в сеть (данные приходят аргументами, БД — через `db`) |
| `services/export.py` | форматы выгрузки (CSV/JSON), парсинг периода, сводка | SQL (запросы — у `DBManager.select_orders`) |
| `autostars/metrics.py` | `/metrics`, `/healthz`, `/status` (свой HTTP на asyncio) | читать БД напрямую (получает снимок), принимать что-то кроме GET |
| `services/task_tracker.py` | журнал событий, зависшие, reconciliation | решать, выдать ли деньги повторно |
| `services/statistics.py` | окна, P&L, отчёты, JSON/TXT | менять статусы заказов |
| `services/bot_control.py` | пауза/резюм, баланс, флаги в БД | — |
| `notifier/tg_alert.py` | исходящие алерты | читать БД (данные приходят аргументами) |
| `notifier/tg_commands.py` | входящие команды владельца, allowlist по chat_id | дублировать CLI-логику (вызывает сервисы) |
| `database/db_manager.py` | схема, миграции, CRUD, агрегаты | бизнес-правил |
| `gui/bridge.py` | запуск/остановка процесса, чтение БД/логов | flet-импортов (тогда тестируется без окна) |
| `gui/app.py` | виджеты, вкладки, обновления UI | прямых обращений к FunPay/GAMEAU/`.env` (кроме `settings`) |

Правило: **движок ничего не знает о GUI**. Обратная связь — только файлы, БД и
вывод процесса. Поэтому CLI и GUI дают одинаковый результат, а_exe не «особенный»_.

## 2. Модель процессов

| Оболочка | Как запускает цикл |
|---|---|
| `autostars` / `python -m autostars.main` | тот же процесс |
| `python autostars_bot.py`, `AutoStarsBot.exe` | обёртка с bootstrap `sys.path` → `autostars.main.main()` |
| GUI (`autostars_gui.py`, `AutoStars.exe`) | `subprocess.Popen(bridge.engine_command())` — отдельный процесс, stdout/stderr читаются в поток-читатель; остановка — SIGTERM → graceful shutdown |
| systemd / Docker | `python -m autostars.main` c `AUTOSTARS_HOME` и `EnvironmentFile` |

Разделение на процессы осознанно: цикл переживает закрытие окна, не зависит от
UI-потока и не может быть «задавлен» перерисовкой. Риск — два цикла на одной базе;
лечится тем, что GUI показывает pid/uptime и не даёт запустить второй
(`EngineProcess.start()` идемпотентен, а на стороне ОС держим один экземпляр).

## 3. Конфигурация

```
Config.load():
  1. config.json   (--config | AUTOSTARS_CONFIG | рядом с exe/проектом)
                   плоские ключи (ENV-имя, атрибут или алиас) и секции API/FUNPAY/BOT/FINANCE/SETTINGS
  2. .env          (AUTOSTARS_ENV | app_dir/.env) → значения инжектируются в os.environ
  3. os.environ    (всё, что не было в файлах, + переменные процесса/systemd/Docker)
  4. пути db_path/log_file резолвятся от app_dir(); parse_errors кладутся в Config
```

- Некорректное число **не роняет** процесс: `_coerce` возвращает дефолт, строка
  уходит в `parse_errors`, её видно в `--check`/`--config-show`/логе.
- Значения-заглушки из примера (`paste_…`, `your_…`) = «не задано» → в `issues()`.
- `_ENV_INJECTED` хранит «последнее значение из файла» для каждого ключа. При
  перечитывании `.env` (кнопка «Сохранить» в GUI) значение обновляется, **только
  если** его не поменял оператор процесса. Это держит баланс между «GUI должен
  видеть новые настройки» и «systemd `Environment=` главнее файла».

## 4. Пути

| Сценарий | app_dir() (запись) | resource_dir() (чтение) |
|---|---|---|
| исходники | корень проекта | корень проекта |
| PyInstaller one-file | папка с exe | `sys._MEIPASS` |
| systemd/Docker | `AUTOSTARS_HOME` | образ |

`resolve_user_path()` делает относительный путь абсолютным от `app_dir()`; `PATH`
из `.env` (`DB_PATH`, `LOG_FILE`) никогда не зависит от текущей папки — иначе
двойной клик по exe создавал бы базу «в непонятно где».

## 5. Поток данных одного заказа

```
FunPay: runner/ long poll (или POLL_INTERVAL-опрос HTML)
   │ новый оплаченный заказ (id, лот, цена ₽, чат node)
   ▼
идемпотентность: заказ уже в orders? ──да──▶ пропуск (без дублей ответа/покупки)
   ▼ нет
save_order_status(RECEIVED) + task_event
   │
parser: @username / t.me/… + количество (иначе DEFAULT_STARS_QUANTITY)
   ├── нет ника → статус WAITING_USERNAME → чат-мониторинг каждые CHAT_MONITOR_INTERVAL_SEC
   ▼
каталог GAMEAU (кеш 60 с) → выбор пакета под количество
   │
maxCharge = max(DEFAULT_MAX_CHARGE_USDT, price × (1 + MAX_CHARGE_MARGIN_PCT/100))
   │
политика выдачи (services/policy.py, если включена): лимит сделки, маржа,
   │  суточный бюджет USDT, дубль за DUPLICATE_WINDOW_MIN, стоп-лист buyer_flags
   ├── есть hold → save_order_status(HOLD_MANUAL, error="CODE: причина") +
   │              EV_RISK_HOLD + критичный TG-алерт (+ REPLY_HOLD покупателю) → Стоп
   └── только alert → EV_RISK_ALERT + TG-алерт (некритичный) → идем дальше
   │
POST /telegramStars  (Idempotency-Key = uuid5(namespace, order_id))
   ├── 409 PRICE_CHANGED → FAILED_PRICE_EXCEEDED, деньги не списаны, алерт
   ├── 429/5xx/timeout  → до MAX_ORDER_RETRIES повторов (тот же ключ идемпотентности)
   └── ок → GAMEAU_CREATED, ждём статус ≤ WAIT_COMPLETION_TIMEOUT
   │
chargedAmount → cost_usdt/cost_rub/profit_rub (факт, не прогноз)
   │
ответ покупателю в чат FunPay (REPLY_DELIVERED) → TG-алерт с прибылью по обоим
курсам (в «тихие часы» — он молчит) → COMPLETED
```

Провал выдачи (`GAMEAU_DELIVERY_FAILED`, `LOW_BALANCE`, `PRICE_EXCEEDED`) тоже
отвечает покупателю — но только если `REPLY_ERROR` непустой: обещание возврата в
чате = публичное обязательство, поэтому по умолчанию там пусто.

Параллельно в цикле (`main.run_bot`):

- **reconciliation** каждые `RECONCILIATION_INTERVAL_SEC`: незавершённые заказы с
  `gameau_order_id` сверяются с GAMEAU (выполнен → закрыть; отклонён →
  `FAILED_DELIVERY`; нет ответа → ждём дальше);
- **детектор зависших**: `STUCK_TASK_MINUTES` без движения → алерт (не чаще раза
  в час на заказ, факт пишется в `task_events`);
- **баланс**: каждые `BALANCE_CHECK_INTERVAL_MIN`, ниже `LOW_BALANCE_THRESHOLD_USDT`
  → алерт;
- **периодическая статистика**: `STATS_PUSH_INTERVAL_MIN` (0 — выкл);
- **TG-команды владельца**: long polling `getUpdates`;
- **снимок метрик** (если `METRICS_ENABLED=true`): раз в 15 с пересобирается из БД и
  конфигурации в `metrics_cache`, HTTP-сервер отдаёт только кэш — scrape не создаёт
  нагрузки на базу и не может замедлить выдачу.

Каждый заказ — отдельная asyncio-задача, не более `MAX_CONCURRENT_ORDERS`
одновременно (семафор).

## 6. Состояние и схема БД

| Таблица | Назначение | Ключи/индексы |
|---|---|---|
| `orders` | состояние выдачи: `order_id`, `gameau_order_id`, `username`, `chat_node`, `quantity`, `price_rub`, `cost_usdt`, `cost_rub`, `profit_rub`, `status`, `error`, `created_ts/updated_ts` | PK `order_id`; `idx_orders_status`, `idx_orders_created_ts` |
| `task_events` | журнал жизненного цикла (для `--tasks`, `--timeline`, детектора зависших) | `idx_task_events_order`, `idx_task_events_ts` |
| `idempotency_keys` / `idempotency_logs` | ключ покупки → заказ, запрос/ответ | PK `idempotency_key` |
| `settings` | флаги цикла (`bot_paused`) и служебные значения | PK `key` |
| `buyer_flags` | стоп-лист покупателей: `username` (с `@`), `kind`, `note`, `created_ts` | PK `(username, kind)`; ищется по `username` |

SQLite-драйвер — `aiosqlite`: запросы исполняет отдельный демонский поток, чтобы
медленный диск (или антивирус на Windows) не блокировал event loop.

Статусы заказа (`db_manager`): в работе — `PROCESSING`, `WAITING_USERNAME`;
успех — `COMPLETED`; провал — `FAILED`, `FAILED_LOW_BALANCE`,
`FAILED_PRICE_EXCEEDED`, `FAILED_DELIVERY`, `CANCELLED` (префикс `FAILED` —
сигнал для `--retry-order`, TG `/retry` и статистики провалов); ожидание человека —
`HOLD_MANUAL` (`HOLD_STATUSES`: не провал и не «в работе», входит в
`RETRYABLE_STATUSES` и в `--export --failed`).

Почему `HOLD_MANUAL` — это отдельный статус, а не `FAILED` и не «в работе»:
оплата на FunPay уже получена (отсюда и `RETRYABLE`), но `FAILED` испортил бы
статистику провалов, а `IN_PROGRESS_STATUSES` — заставил бы reconciliation каждые
`STUCK_TASK_MINUTES` кричать «задача зависла» на каждый задержанный заказ.

Статусы
`RECEIVED`, `PACKAGE_SELECTED`, `FUNPAY_REPLY`, … — это **события** журнала
`task_events`, а не состояния заказа: по ним строится `--timeline`.

Прагмы (`PRAGMAS`): `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`,
`foreign_keys=ON`.

Миграции — только через `DBManager._migrate_orders_table()` (добавляет недостающие
колонки через `ALTER TABLE`); удаление/переименование колонок запрещено: база у
пользователей переживает обновления.

## 7. Остановка (graceful shutdown)

Порядок фиксирован (`main.run_bot`, `finally`):

1. стоп-флаг (`SIGTERM`/`SIGINT` через `loop.add_signal_handler`, `install_stop_handlers()`,
   TG-команда `/stop`, остановка из GUI);
2. `asyncio.wait(gather(tasks), timeout=SHUTDOWN_TIMEOUT_SEC)` — начатые выдачи
   доживают до статуса; не дожившие — `cancel()`;
3. `stop_event.set()` — цикл и TG-_polling завершаются;
4. закрытие HTTP-клиентов и нотифайера;
5. алерт «🛑 AutoStars остановлен» (если Telegram настроен);
6. закрытие БД.

`TimeoutStopSec` в systemd и `stop_grace_period` в compose заведомо больше
`SHUTDOWN_TIMEOUT_SEC` — иначе процесс убьют посреди покупки.

## 8. Обработка ошибок

| Слой | Политика |
|---|---|
| HTTP к GAMEAU | повторы на 429/502/503/504/таймаут (`GAMEAU_MAX_RETRIES`, `MAX_ORDER_RETRIES`) с тем же `Idempotency-Key`; бизнес-ошибки — не повторяем |
| FunPay 403/419 | одно обновление CSRF + повтор; persistent-сессия с `User-Agent` |
| логин при старте | бесконечные (или `STARTUP_MAX_RETRIES`) попытки с `STARTUP_RETRY_DELAY` — чтобы `Restart=always` не раздувало рестарты |
| любая ошибка заказа | статус `FAILED*` + `error` + `task_event` + алерт; цикл живёт дальше |
| ошибка в служебной подзадаче (reconcile, баланс, статистика) | лог `[ERR]` и продолжение — они не критичны для выдачи |
| некорректная конфигурация | дефолт + `parse_errors`; `--check`/лог показывают причину |

Никогда не глотаем `Exception` без лога, и никогда не пишем в чат покупателю
текст исключения — там только нейтральная формулировка, детали уходят владельцу.

## 9. Инварианты (менять нельзя)

1. **Один заказ FunPay = не более одной покупки звёзд.** Ключ — `uuid5(NAMESPACE_DNS, f"funpay_{order_id}")`, то есть детерминирован
   по номеру заказа, поэтому ретрай безопасен; повторный заказ с записью в `orders`
   не обрабатывается.
2. **`maxCharge` ≥ цены пакета и ≤ цены + запас.** Ниже — отказ `INSUFFICIENT…`,
   выше — `PRICE_CHANGED` без списания.
3. **Статистика считается по факту** (`chargedAmount`, `price_rub`), а не по
   прогнозу курса: прибыль в отчёте = реальные деньги.
4. **Ни один секрет не попадает в вывод**: логи, алерты, `--config-show`, GUI-форма.
5. **GUI не меняет состояние движка иначе как через CLI/`.env`** — иначе
   расхождение «в окне одно, в консоли другое» неизбежно.
6. **Проверки политики считаются до запроса в GAMEAU, а не после.** Списанные USDT
   обратно не вернуть; `hold` = «не покупаем», а «что делать с заказом» решает
   человек (`--release`/`--cancel`). Обход политики разрешён только явным
   `ignore_policy` из ручных команд (`--release`, `/release`) — автоматический путь
   его никогда не включает.
7. **Задержанный заказ не считается провалом и не считается зависшим** (см. §6):
   `HOLD_MANUAL` вне `IN_PROGRESS_STATUSES` и вне префикса `FAILED`.
8. **Метрики не включают бизнес-логики**: снимок считает цикл и отдаёт кэш; сервер
   слушает localhost и выключен по умолчанию (см. `docs/SECURITY.md`, §7).

## 10. Границы модели

- Один экземпляр на базу: нет распределения/лок-файла; два цикла = дубли ответов.
- SQLite-файл должен быть на локальном диске (WAL плохо переносит сетевые ФС).
- «Календарный день» в статистике — по локальному времени процесса; в Docker/VC
  следите за `TZ`.
- Цены и наличие пакетов — на стороне GAMEAU; каталог кешируется 60 с, поэтому
  резкое изменение цены увидит `maxCharge`, а не «купит дороже».
- Нет хранения переписки и персональной аналитики: в БД только то, что нужно для
  выдачи (ник, номер чата, сумма).
- Legacy (`legacy/`) — отдельная вселенная: `db.json`, синхронные запросы; новые
  фичи туда не добавляются, совместимость не гарантируется.
