# 📚 Источники и ссылки

## 1. Откуда взялся этот проект

| Проект | Что дали | Что у нас иначе |
|---|---|---|
| [Vamp1reAchao/AutoStars](https://github.com/Vamp1reAchao/AutoStars) — референс структуры | идею «один движок + GUI + тесты + `build.bat`/`install.bat`/`check_env.py` + `docs/` и `FEATURES/INSTALL/CHANGELOG/CONTRIBUTING`», `pyproject.toml`, GitHub Actions | тот же список доведён до рабочего состояния: пакет устанавливается (`pip install -e .`), GUI запускается и тестируется, CI проверяет не только `py_compile` |
| `legacy/` (в этом репозитории) — исходный AutoStars-New | парсинг FunPay, схема «купил → выдал», `db.json` | `legacy/` изолирован: не запускается из корня, не тянет зависимости ядра, не участвует в CI как блокирующий |
| [GAMEAU API docs](https://gameau.us/api-docs.html) | `POST /orders/telegramStars`, `GET /catalog`, `GET /orders/status/{id}`, `GET /account`, поля `hide_sender`, `maxCharge`, `chargedAmount`, заголовок `Idempotency-Key` | каталог кешируется 60 с; статус ждём с таймаутом; повторяем только `429/5xx` и таймауты; пути перебираем (`/v1` и без) |
| FunPay (публичный сайт) | страница оплаченных заказов, `runner/` long polling, чат, `golden_key` cookies, csrf-токен в HTML | 403/419 → одно обновление csrf и повтор; `FUNPAY_USER_AGENT` настраивается |

Важно про референс: его CI ограничивается `py_compile` GUI-файла, а GUI там
зависит от мёртвых импортов — поэтому «как у них» не самоцель; сверяли список
файлов и формат документов, а не код.

## 2. Версии и зависимости

| Пакет | Диапазон | Где задан | Комментарий |
|---|---|---|---|
| Python | ≥ 3.10 | `pyproject.toml` (`requires-python`), CI 3.10/3.11/3.12 | на 3.12+ flet 0.86 местами требует обновлений |
| `httpx` | `>=0.27,<1` | `pyproject.toml` | async-клиент + пул соединений |
| `aiosqlite` | `>=0.20,<1` | `pyproject.toml` | SQLite без блокировки event loop |
| `beautifulsoup4` | `>=4.12,<5` | `pyproject.toml` | парсинг HTML FunPay |
| `flet` | `>=0.86.1,<1` | extra `gui` | 1.0 — ломающий API; 0.86.x — на чём написан и проверен GUI |
| `pytest`, `anyio`, `pytest-timeout`, `ruff` | `>=…` | extra `dev` | тесты ядра не требуют flet |
| `pyinstaller` | `>=6.6` | extra `build` | сборка `AutoStarsBot.exe` |
| `FunPayAPI` | `==1.1.0`, `--no-deps` | `legacy/requirements.txt` | только для legacy; версия 1.0.4.20 на PyPI отсутствует — не ставьте её |

Установка: `pip install -e .` (ядро) · `pip install -e ".[gui]"` ·
`pip install -e ".[dev]"` · `pip install -e ".[gui,build]"`.

## 3. Концы API, которые трогает движок

```
FunPay (https://funpay.com; cookies golden_key + csrf из HTML, HTML парсит BeautifulSoup)
  GET  /orders/trade?state=paid                 таблица оплаченных заказов (№, лот, цена, node чата)
  POST /runner/                                 long polling и действия:
                                                chat_message (ответ покупателю), обновление чатов;
                                                тело: objects/request = JSON-строки
  GET  /chat/history?node=<id>&last_message=…   переписка: @username и количество звёзд

GAMEAU (base = GAMEAU_BASE_URL; клиент пробует путь и с /v1, и без — см. _candidate_urls)
  GET  catalog                                  пакеты telegramStars: name/price/orderable (кеш 60 c)
  POST orders/telegramStars  (fallback: telegramStars)
                                                username, quantity, maxCharge, hide_sender,
                                                заголовок Idempotency-Key
  GET  orders/status/{id} | status/{id} | orders/{id}
                                                статус + фактический chargedAmount
  GET  account                                  баланс USDT (для --balance и алертов)

Telegram (https://api.telegram.org/bot<token>)
  POST sendMessage     алерты и отчёты владельцу
  GET  getUpdates      команды владельца (long polling, без вебхука и открытых портов)
```

Ответы API трактуются защитно: имя поля могут отдать как `id`/`orderId`/`order_id`,
баланс — как `balance`/`balanceUsdt`/`amount`, каталог — как `items` или
`catalog.items`. Правки здесь обязательны, когда GAMEAU меняет схему: движок
подхватит familiar-варианты, но новую схему надо добавить в клиенты и в тесты.

## 4. Документация и практики, на которые ориентирован код

- [PyInstaller manual](https://pyinstaller.org/en/stable/) — spec-файлы, `datas`,
  onefile/onedir, антивирусные ложные срабатывания.
- [flet docs](https://docs.flet.dev/) и `flet pack --help` — `Page`, `Tabs`,
  `DataTable`, `window.*`, `flet pack -n -i --add-data -y`.
- [SQLite WAL](https://www.sqlite.org/wal.html) и
  [busy_timeout](https://www.sqlite.org/pragma.html#pragma_busy_timeout) — почему
  GUI и цикл могут читать/писать одну базу.
- [aiosqlite](https://aiosqlite.omnilib.dev/) — поток-исполнитель запросов.
- [Idempotency keys (Stripe post)](https://stripe.com/blog/idempotency) —
  идея детерминированного ключа на операцию; у нас `uuid5(NAMESPACE_DNS, "funpay_<id>")`.
- [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
  [SemVer](https://semver.org/lang/ru/) — формат `CHANGELOG.md`.
- [12-factor config](https://12factor.net/config) — конфигурация из окружения
  процесса (+ файлы), отсюда приоритет «процесс > `.env` > `config.json`».
- [OWASP Secrets Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html) —
  маскирование, ротация, «не в аргументах процесса», отсюда `security.py` и политика `.env`.

## 5. Известные внешние «грабли» (их учли в коде)

| Грабли | Что сделано |
|---|---|
| FunPay отдаёт 403 без «браузерного» User-Agent / при просроченном csrf | `FUNPAY_USER_AGENT`, автообновление csrf + один повтор, тесты |
| GAMEAU меняет цену пакета между запросом и покупкой | `maxCharge` от цены каталога + `MAX_CHARGE_MARGIN_PCT`, 409 → без списания |
| двойная покупка при ретраях | `Idempotency-Key` + проверка заказа в `orders` |
| «бот молчит, потому что `.env` не тот» | `--config-show` с источниками, `check_env.py`, `AUTOSTARS_HOME` |
|exe «не видит» настройки (cwd ≠ папка exe) | `autostars/paths.py` |
| процесс умирал от `POLL_INTERVAL=abc` | `_coerce` + `parse_errors` |
| GUI писал «свои» значения и расходился с CLI | GUI меняет только `.env`/флаги через CLI-команды; тесты `test_gui_*` |
| flet 1.0 ломает API | зависимость закреплена `<1` |
| CI, где тесты идут по `legacy/`, падал на импортах | `testpaths = ["tests"]`, отдельный неблокирующий job для legacy |

## 6. Где искать ответы дальше

- команды и флаги: `autostars --help`, `autostars --check`, `--config-show`
- устройство и инварианты: [ARCHITECTURE.md](ARCHITECTURE.md)
- секреты, деньги, права: [SECURITY.md](SECURITY.md)
- тесты и ручная проверка: [TESTING.md](TESTING.md)
- установка и типовые сбои: [../INSTALL.md](../INSTALL.md)
- сборка под Windows: [WINDOWS_BUILD.md](WINDOWS_BUILD.md)
- история изменений: [../CHANGELOG.md](../CHANGELOG.md)
- legacy-реализация и миграция с `db.json`: [../legacy/README.md](../legacy/README.md)
- код: `autostars/` (движок) → `autostars/main.py` (CLI/цикл) →
  `autostars/services/order_processor.py` (выдача)
