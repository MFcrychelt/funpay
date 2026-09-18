# 🌟 Возможности AutoStars

Документ о том, **что именно** делает программа, **чем это управляется** и **где
это лежит/тестируется**. Числа и названия параметров совпадают с `.env.example`
(за этим следит `tests/test_config_drift.py`).

## 1. Канал FunPay (приём заказов)

| Что | Как |
|---|---|
| Список оплаченных заказов | HTML-раздел `buyer/orders` + `beautifulsoup4`, фильтр «ждут ответа» |
| Мгновенное событие | long polling `runner/` — бот видит новый заказ почти сразу, а не через `POLL_INTERVAL` |
| CSRF | токен берётся из страницы, при `403/419` автоматически обновляется и запрос повторяется один раз |
| Ответ покупателю | текст из шаблона лота + `@username`/количество, повторная отправка по тому же заказу исключена |
| Доступ | `golden_key` продавца из cookies; `FUNPAY_USER_AGENT` — если FunPay отвечает 403 |

Модуль: [`autostars/clients/funpay.py`](autostars/clients/funpay.py) ·
тесты: `tests/test_autostars.py` (long polling, CSRF), `tests/test_cli_smoke.py`.

## 2. Канал GAMEAU (покупка и выдача звёзд)

- каталог пакетов Telegram Stars с кешированием 60 с — выбор минимального
  подходящего пакета под нужное количество звёзд;
- `POST /telegramStars` с `Idempotency-Key` (UUID v5 от номера заказа FunPay) —
  повторный запрос **не создаёт** вторую покупку;
- `maxCharge` = `max(DEFAULT_MAX_CHARGE_USDT, цена пакета × (1 + MAX_CHARGE_MARGIN_PCT/100))` —
  если GAMEAU поднимет цену выше лимита, ответ `409 PRICE_CHANGED` и **деньги не списываются**;
- после покупки — ожидание финального статуса (`WAIT_COMPLETION_TIMEOUT`) и
  фактическая сумма списания `chargedAmount` (она идёт в статистику, а не «примерно»);
- ретраи только на сетевые сбои (429/502/503/504, таймауты), бизнес-отказ не
  повторяется — иначе возможен двойной заказ;
- `hide_sender` (`HIDE_SENDER`), баланс и порог алерта (`LOW_BALANCE_THRESHOLD_USDT`).

Модуль: [`autostars/clients/gameau.py`](autostars/clients/gameau.py) ·
тесты: `tests/test_autostars.py`, `tests/test_v22.py`.

## 3. Разбор сообщений покупателя

`@username`, `t.me/username`, «1000 звёзд / 1000⭐ / 1к», несколько звёзд в одном
сообщении, невалидные никнеймы (меньше 5 символов, не-latin) — всё в
[`autostars/services/parser.py`](autostars/services/parser.py). Если количество
не распознано — `DEFAULT_STARS_QUANTITY` (1000).

**Чат-мониторинг**: заказ в статусе `WAITING_USERNAME` перечитывается каждые
`CHAT_MONITOR_INTERVAL_SEC` (15 с); как только покупатель написал ник в чат —
выдача стартует автоматически, повторных просьб нет.

## 4. Надёжность цикла

| Механизм | Что даёт |
|---|---|
| Идемпотентность по БД | заказ, уже записанный в `orders` (включая `FAILED*`), не уходит в пайплайн повторно — никаких дублей ответов и покупок |
| Reconciliation | каждые `RECONCILIATION_INTERVAL_SEC` (60 с) незавершённые заказы сверяются с GAMEAU: выполнился → закрыть сделку; отклонён → `FAILED_DELIVERY` с фактической стоимостью |
| Детектор зависших | `STUCK_TASK_MINUTES` (20) без движения → алерт владельцу (не чаще раза в час на заказ) + запись в журнал |
| Семафор выдачи | не более `MAX_CONCURRENT_ORDERS` (4) параллельных покупок, ожидание статуса не блокирует очередь |
| Graceful shutdown | `SIGTERM`/`/stop` → дождаться начатых выдач до `SHUTDOWN_TIMEOUT_SEC` (30 с), затем закрыть БД; Ctrl+C работает так же |
| Старт с ретраями | нет сети/503 на входе → цикл перезапускается каждые `STARTUP_RETRY_DELAY` (15 с); `STARTUP_MAX_RETRIES=0` = вечно (удобно с `Restart=always`) |
| Пауза/резюм | флаг в БД: переживает рестарт процесса, не бросает начатые заказы |
| Ретрай провала | `--retry-order <ID>` или `/retry <ID>` — повторно, но идемпотентно |
| WAL в SQLite | `journal_mode=WAL`, `busy_timeout`, `foreign_keys` — GUI и цикл не блокируют друг друга |

Модули: [`services/order_processor.py`](autostars/services/order_processor.py),
[`services/task_tracker.py`](autostars/services/task_tracker.py),
[`services/bot_control.py`](autostars/services/bot_control.py),
[`database/db_manager.py`](autostars/database/db_manager.py).

## 5. Статистика, P&L, отчётность

Окна: **1 ч, 2 ч, 3 ч, 4 ч, 6 ч, 24 ч, календарный день (с 00:00 сервера), всего**
+ свои окна `--stats-hours 1,6,24`.

По каждому окну: заказы (всего/готово/провал/в работе/ждут ник), звёзды,
выручка ₽, затраты USDT и ₽ (по активному курсу + `TRON_ENERGY_FEE_RUB`),
прибыль ₽, маржа %, средняя и лучшая сделка, **убытки** (сделки с маржей < 0),
потерянная выручка по проваленным заказам, списанные без выдачи средства.

`--report` добавляет P&L за 24 часа, топ сделок и список провалов с причинами;
`--stats --json` / `--report --json` — машиночитаемый вывод (графа, ноды,
свой дашборд). `STATS_PUSH_INTERVAL_MIN=60` — авто-отправка в Telegram,
`--stats-push` — вручную.

Модуль: [`autostars/services/statistics.py`](autostars/services/statistics.py) ·
тесты: `tests/test_statistics.py` (окна, убытки, маржа).

## 6. Журнал задач

Каждый заказ — задача с событиями в `task_events`:

```
RECEIVED → PARSED_OK/PARSED_FAIL → PACKAGE_SELECTED → GAMEAU_CREATED
         → GAMEAU_COMPLETED/GAMEAU_FAILED → FUNPAY_REPLY → TG_ALERT
         → COMPLETED/FAILED     (+ WAITING_USERNAME, STUCK_ALERTED, RETRY, CANCELLED)
```

`--tasks` — открытые/зависшие + последние события, `--timeline <ID>` — полная
хронология одного заказа (её же видит TG-команда `/tasks`). Это то, что экономит
час переписки с поддержкой GAMEAU: видно, ушёл ли запрос и что именно ответило.

## 7. Telegram-уведомления и управление с телефона

- алерт на каждую сделку: получатель, звёзды, цена, списанные USDT, **прибыль по
  обоим курсам**, статус;
- критические события: `LOW_BALANCE`, `PRICE_EXCEEDED`, `NETWORK`, `FAILED_DELIVERY`,
  зависшая задача, старт/остановка цикла;
- интерактивный бот владельца (long polling, без вебхука и открытых портов):
  `/help` `/status` `/stop` `/pause` `/resume` `/stats` `/report` `/tasks`
  `/balance` `/retry <id>` `/calc <₽> [звёзды]`;
- `chat_id` берётся из `TELEGRAM_CHAT_ID`, чужие сообщения игнорируются;
- в текстах алертов секреты не появляются (`autostars/security.py`).

Модули: [`notifier/tg_alert.py`](autostars/notifier/tg_alert.py),
[`notifier/tg_commands.py`](autostars/notifier/tg_commands.py).

## 8. Конфигурация и диагностика

- **.env** (главный источник), **config.json** (для Docker/systemd/ansible),
  переменные процесса — приоритет `процесс > .env > config.json > код`;
- значения вне диапазона и мусор в числах не роняют движок: они попадают в
  `parse_errors`, их видно в `--check` и `--config-show`;
- пути (`.env`, БД, лог) привязаны к папке exe/проекта, переопределяются
  `AUTOSTARS_HOME` / `AUTOSTARS_ENV` / `--config` — поэтому двойной клик по exe
  и systemd-юнит работают с одними и теми же файлами;
- `check_env.py` — проверка Python, пакетов, прав, конфигурации и ключей перед
  первым запуском;
- `--check` — конфиг → SQLite → FunPay → GAMEAU → каталог → курсы → Telegram;
- `--config-show` — что реально применилось и откуда (секреты замаскированы).

Модули: [`autostars/config.py`](autostars/config.py),
[`autostars/paths.py`](autostars/paths.py),
[`check_env.py`](check_env.py) · тесты: `tests/test_config.py`,
`tests/test_security_paths.py`, `tests/test_config_drift.py`.

## 9. Настольный интерфейс (flet)

Шесть вкладок, все данные — из того же процесса/БД/логов, что у CLI:

| Вкладка | Что внутри |
|---|---|
| **Главная** | статус цикла (запущен/остановлен, pid, uptime, код выхода), плитки по статусам заказов, курс и вариант завода, предупреждения о конфигурации; кнопки «▶ Запустить цикл», «⏹ Остановить», «⏸ Пауза приёма», «▶ Возобновить», «🔍 Диагностика (--check)», «💰 Баланс GAMEAU», «📦 Каталог» |
| **Статистика** | таблица окон (как `--stats`), полный отчёт `--report`, прибыль и маржа |
| **Заказы** | последние заказы из SQLite (ник, звёзды, цена, USDT, ₽, прибыль, статус, ошибка), кнопка «🧭 Зависшие задачи (--tasks)» |
| **Настройки** | 38 сгруппированных полей с подсказками, проверкой чисел/диапазонов, «💾 Сохранить в .env» (с бэкапом `.env.bak`, комментарии и порядок строк сохраняются), «↺ Перечитать» |
| **Логи** | хвост `autostars.log` + вывод дочернего процесса, автообновление |
| **Диагностика** | `check_env.py`, `--check`, `--config-show`, пути к `.env`/БД/логу, версия, предостережения |

Гарантии, закреплённые кодом:

- GUI **не** разговаривает с FunPay/GAMEAU сам — он запускает `python -m autostars.main`
  (или `AutoStarsBot.exe` в сборке) и читает результат;
- секреты не показываются в форме и не пишутся в логи; пустое поле секрета = «не менять»;
- цикл живёт в отдельном процессе и переживает закрытие окна; остановка — SIGTERM с
  graceful shutdown, а не `kill` посреди покупки;
- логика моста (`autostars/gui/bridge.py`) и настроек (`settings.py`) не зависит от flet —
  поэтому тестируется без окна: `tests/test_gui_bridge.py`, `tests/test_gui_settings.py`;
  сборку интерфейса проверяет `tools/gui_smoke.py`.

## 10. Дистрибуция и эксплуатация

- `install.bat` — venv + `pip install -e ".[gui]"` + `.env` из примера + `check_env.py`;
- `start_gui.bat` / `run_bot.bat` — запуск без активации venv;
- `build.bat` + `autostars.spec` — `dist\AutoStarsBot.exe` (PyInstaller, one-file) и
  `dist\AutoStars.exe` (GUI через `flet pack`);
- `Dockerfile` + `docker-compose.yml` — non-root, `AUTOSTARS_HOME=/app/data`,
  healthcheck `--check`, graceful stop 60 с;
- `deploy/autostars.service` — systemd с `EnvironmentFile`, `Restart=always`,
  `TimeoutStopSec=90`, `ProtectSystem=full`;
- CI [`ci/github-ci.yml`](ci/github-ci.yml) — ruff, 3.10/3.11/3.12, смоук CLI, GUI-смоук,
  legacy-тесты, проверка ссылок документации, `docker build`; включается копированием в
  `.github/workflows/ci.yml` ([ci/README.md](ci/README.md)).

## 11. Чего здесь сознательно нет

- вебхуков, админ-панели и «бота для чата с клиентами» — только исходящие соединения;
- работы со старой `db.json` внутри движка: чтение — за legacy-утилитами,
  перенос заказов в таблицу `orders` — одноразовый ([legacy/README.md](legacy/README.md));
- «умного» ценообразования и авто-выставления лотов: цена и текст лота — на FunPay,
  программа только выдаёт оплаченное;
- GUI-настроек, которых нет в движке: поле в форме появляется только вместе с
  реальным параметром `Config` (иначе тест дрейфа падает).
