# 🧪 Тестирование AutoStars

## 1. Как запускать

```bash
pip install -e ".[dev]"          # pytest, anyio, pytest-timeout, ruff

python -m pytest -q                     # 154 теста: движок + CLI + GUI-мост + конфигурация
python -m pytest tests/test_config.py -q      # один файл
python -m pytest tests/test_gui_bridge.py::test_engine_process_start_stop -q
python -m pytest -q -k "not smoke"          # без долгого (CLI-смоук спавнит процессы)

python -m pytest legacy/tests -q     # 45 тестов legacy (нужен FunPayAPI, см. §5)
python tools/gui_smoke.py            # GUI собирается и рендерит вкладки без окна
python -m ruff check autostars tests tools check_env.py
python -m compileall -q autostars    # синтаксис под целевую версию Python
```

Всё это делает CI ([`ci/github-ci.yml`](../ci/github-ci.yml)) на Python 3.10/3.11/3.12 —
локальный прогон должен давать тот же результат. Сам workflow не включён в
`.github/workflows/` (правка таких файлов требует отдельного права GitHub) —
включается одной командой, см. [ci/README.md](../ci/README.md).

## 2. Карта тестов

По файлам (154 теста): `test_autostars` 38 · `test_config` 24 · `test_cli_smoke` 19 ·
`test_v22` 18 · `test_gui_bridge` 13 · `test_statistics` 13 · `test_gui_settings` 12 ·
`test_security_paths` 11 · `test_config_drift` 6.

| Файл | Что охраняет | Ключевые проверки |
|---|---|---|
| `tests/test_config.py` | конфигурацию | приоритеты «процесс > `.env` > `config.json` > код», заглушки = «не задано», битые числа → `parse_errors` без падения, `.env` после сохранения GUI перечитывается, `config.json` в трёх формах ключей |
| `tests/test_config_drift.py` | согласованность документации и кода | `.env.example` ↔ `Config` ↔ поля GUI: ни одно значение по умолчанию не разъехалось; каждый ключ примера понимает движок |
| `tests/test_security_paths.py` | секреты и пути | redact шаблонов (кук, JSON, HTML meta, TG-токен в URL), маскирование, `AUTOSTARS_HOME`/`AUTOSTARS_ENV`, независимость от `cwd` |
| `tests/test_autostars.py` | ядро | long polling и CSRF FunPay, парсер никнеймов/звёзд, `maxCharge` и `Idempotency-Key` в GAMEAU-клиенте, пайплайн `order_processor`, пауза/ретрай/баланс |
| `tests/test_statistics.py` | отчётность | окна времени, маржа, убытки, потерянная выручка, `chargedAmount`, миграция старой базы, JSON/TXT-рендер |
| `tests/test_v22.py` | «отзывчивость» 2.2 | чат-мониторинг, персистентные соединения, защита от повторной обработки, TG-сервер команд (allowlist, dispatch) |
| `tests/test_cli_smoke.py` | интерфейс командной строки | реальный `python -m autostars.main` в изолированной папке: коды возврата, `--json`, `--config-show` без секретов, `--pause/--resume` пишут в БД, `--test-order` без `-y` не запускается, первый запуск создаёт `.env` и не притворяется успехом |
| `tests/test_gui_bridge.py` | мост GUI ↔ движок | команда строится правильно (исходники/exe), разовые команды выполняются, `EngineProcess` запускает/останавливает реальный процесс и собирает логи, чтение БД не пишет в неё, старые схемы базы не роняют GUI |
| `tests/test_gui_settings.py` | редактор настроек | валидация чисел/диапазонов/выборов, сохранение комментариев и порядка, `.env.bak` только при реальном изменении, секрет не затирается пустым полем, все ключи примера имеют поле |
| `legacy/tests/*` | исторический код | синхронный `bot/`: история, парсер, состояние, `db.json` — бегают «как есть» |

## 3. Фикстуры (`tests/conftest.py`)

- **`isolated_env`** — временный каталог в `AUTOSTARS_HOME`, указатель `AUTOSTARS_ENV`
  на несуществующий `.env`, `AUTOSTARS_CONFIG` — в ту же папку, и **удаление всех
  переменных окружения из `ENV_MAP`**. Без последней части тесты «протекают»:
  значение, инжектированное из `.env` предыдущим тестом, молча переопределяет
  конфигурацию следующего (это и был реальный баг GUI-сохранения, найденный тестом).
- **`anyio_backend`** — `asyncio` для `async def`-тестов.

Тест, который трогает файлы или конфигурацию, обязан брать `isolated_env`
(или `tmp_path` + явные `monkeypatch.setenv`). Тесты, которым нужен «боевой»
`.env`/реальная база, не принимаются — они падают на CI и у пользователей.

## 4. Правила

1. **Никакой сети.** Мокаем на уровне клиента: подмена метода (`async def fake_post`)
   или `httpx.MockTransport`. Если код ходит в сеть «в обход» клиента — это дефект
   дизайна, правьте код, а не тест.
2. **Никаких реальных денег.** `--test-order` тестируется только с `--dry-run` или без
   `-y` (тогда проверяется код возврата 2 и отсутствие HTTP-запроса).
3. **Псевдо-ключи** вида `fake-key-for-tests`; проверка «ключей нет» использует
   значения-заглушки из примера, а не пустые строки — иначе не проверяется тот же
   путь, что видит новичок.
4. **Стабильность важнее полноты**: никаких `sleep(1)` «на всякий случай» — для
   ожидания состояния процесса используйте цикл с дедлайном (см.
   `test_engine_process_start_stop`), для времени — `time.time()` до/после или
   явные таймстемпы в БД.
5. **Докстринг теста = причина существования.** «`assert result is not None`»
   без объяснения не принимает.
6. **Тест на дефект** — сначала падал бы без правки. Хороший пример:
   `test_applied_values_reach_the_engine` (без фикса `_ENV_INJECTED` он красный).

## 5. Legacy-тесты

`legacy/tests` требуют `FunPayAPI` и не используют пакет `autostars`:

```bash
python -m pip install -r legacy/requirements.txt
python -m pytest legacy/tests -q
```

В CI `FunPayAPI` ставится с `--no-deps`: у пакета завышены требования к
`requests`, а legacy-коду нужен только парсинг. Джоб `legacy` не блокирует
сборку (`continue-on-error: true`) — легаси поддерживается «на уровне сборки»,
его правки не ожидаются.

## 6. Как тестировать GUI

Окно flet в CI не запускается (нет дисплея, а Flutter-рантайм тянет ~70 МБ).
Поэтому интерфейс разрезан:

- `bridge.py` и `settings.py` — чистая логика, тесты гоняются без flet;
- `app.py` — виджеты; его собирает `tools/gui_smoke.py`: строит все вкладки,
  прогоняет рендер таблиц/статуса, собирает и валидирует значения 38 полей
  настроек. Падение — значит «пользователь увидит пустое окно или Traceback».

Локально полный прогон:

```bash
pip install -e ".[gui]"
python autostars_gui.py            # окно; в CI этого нет
python tools/gui_smoke.py          # то же, но без запуска окна
```

Отдельно стоит проверить «живой» цикл из GUI: «▶ Запустить цикл» → «Логи» показывает
строки движка → «⏹ Остановить» завершает процесс (код выхода виден на «Главной»).

## 7. Ручная проверка перед релизом (5 минут)

```bash
export AUTOSTARS_HOME=/tmp/autostars-check && rm -rf $AUTOSTARS_HOME && mkdir -p $AUTOSTARS_HOME
python autostars_bot.py --check          # 1) создал .env, честно ругнулся на пустые ключи (rc=1)
python check_env.py                      # 2) окружение и пути
# 3) заполните /tmp/autostars-check/.env реальными ключами
python autostars_bot.py --check          # 4) FunPay/GAMEAU/БД/курсы — без [ERR]
python autostars_bot.py --catalog | head # 5) каталог и себестоимость
python autostars_bot.py --test-order ваш_ник --dry-run
python autostars_bot.py --test-order ваш_ник --test-qty 20 -y
python autostars_bot.py --stats --json | python -m json.tool | head -20
python autostars_bot.py --once -v        # 6) один проход цикла в отладочном логе
```

И на Windows-машине: `install.bat` → `start_gui.bat` → запуск/остановка цикла →
`build.bat` → `dist\AutoStarsBot.exe --check`.

## 8. Покрытие и метрики

Цель — не процент строк, а «каждый денежный и восстановительный путь имеет тест»:
идемпотентность, `maxCharge`, reconciliation, пауза/ретрай, обработка 403/TLS,
окна статистики, коды возврата CLI, перечитывание `.env`.

Локальный профиль покрытия (по желанию):

```bash
pip install pytest-cov
python -m pytest --cov=autostars --cov-report=term-missing -q
```

В CI coverage не собирается: он ничего не решает, а ложное чувство «покрыто» мешает.
