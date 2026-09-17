# AutoStars (FunPay <-> Gameau Engine)

Современный, полностью асинхронный и отказоустойчивый стек автовыдачи **Telegram Stars** на [FunPay](https://funpay.com) с интеграцией B2B API **Gameau** и финансовой моделью **Whitebird USDT (+ TRON Energy)**.

---

## 1. Архитектурные преимущества новой версии

| Проблема старых версий | Риск / Последствия | Решение в новой архитектуре AutoStars |
| :--- | :--- | :--- |
| **Синхронный `requests` / `time.sleep`** | Пропуск заказов при высокой активности, задержка ответа 10–30 сек. | **`asyncio` + `httpx` (Connection Pool, HTTP/2)** — параллельная неблокирующая обработка. |
| **Отсутствие `Idempotency-Key`** | Сетевой сбой во время запроса = **двойная покупка Stars** и потеря USDT. | **UUID v5/v4 Idempotency Key** на каждый `order_id` в SQLite/`aiosqlite`. |
| **Отсутствие контроля цены (`maxCharge`)** | Поставщик поднял цену = **списание баланса в минус**. | Передача параметра `maxCharge` в Gameau API и превентивная остановка сделки. |
| **Жесткий / ненадежный Regex** | Покупатель написал `t.me/nick` или `@nick_1` — скрипт клинит. | Нормализация юзернеймов с авто-фоллбэком и запросом уточнения в чат FunPay. |
| **Устаревший CSRF на FunPay** | Блокировка за частые запросы страниц или просроченный токен. | Динамическое извлечение `csrf_token` из `data-app-data` + автообновление при `403 Forbidden`. |

---

## 2. Структура проекта

```
autostars/
├── config.py             # Загрузка переменных (golden_key, gameau_key, telegram_bot_token)
├── database/
│   ├── models.py         # AIOSQLite схемы (orders, idempotency_logs, settings)
│   └── db_manager.py     # Асинхронные методы CRUD
├── clients/
│   ├── funpay.py         # Long Polling runner/, extraction csrf_token, postMessage, 403 recovery
│   └── gameau.py         # B2B API client, maxCharge, retry with exponential backoff
├── services/
│   ├── parser.py         # Regex engine для Telegram handles (ссылки, @тэги, фоллбэк)
│   └── order_processor.py# Главная бизнес-логика (Оплата FP -> Parsing -> Gameau -> Ответ FP -> TG Alert)
├── notifier/
│   └── tg_alert.py       # Telegram бот для уведомления владельца (алерты прибыли и баланса USDT)
└── main.py               # Точка входа (Async loop)
```

---

## 3. Финансовая модель (Whitebird USDT + TRON Energy)

Расчёт чистой прибыли по каждой сделке производится по формуле:

$$\text{Себестоимость} = (\text{USDT Cost} \times \text{Курс Whitebird}) + \text{TRON Energy Fee}$$

$$\text{Чистая прибыль} = \text{FunPay Выручка (RUB)} - \text{Себестоимость}$$

* **Пример:** 1 000 Stars = 9.10 USDT.
* При курсе Whitebird **87.63 RUB/USDT** себестоимость составляет $9.10 \times 87.63 = 797.43$ RUB.
* При продаже лота на FunPay за 1 370.40 RUB чистая прибыль составляет **+572.97 RUB**.
* При недостатке баланса USDT бот мгновенно отправляет критический алерт владельцу в Telegram с напоминанием пополнить кошелёк через Whitebird.

---

## 4. Чек-лист миграции с AutoStars

- [x] **Шаг 1: Извлечение конфигов.** Скопировать `golden_key` из браузера и API-ключ с личного кабинета Gameau.
- [x] **Шаг 2: Настройка базы данных SQLite.** Создать таблицы `orders` (поля: `order_id`, `username`, `status`, `created_at`) и `idempotency_keys` / `idempotency_logs`.
- [x] **Шаг 3: Тестирование метода Long Polling (`runner/`).** Обеспечить автопереполучение `csrf_token` при ответе `403 Forbidden`.
- [x] **Шаг 4: Тест парсера утилитой unit-тестов.** Прогнать тестовые строки реальных покупателей через модуль `parser.py`.
- [x] **Шаг 5: Интеграция с Telegram Alert Ботом.** Настроить уведомления о каждой успешной сделке с калькуляцией чистой прибыли (по схеме Whitebird 87.63 RUB).
- [x] **Шаг 6: Запуск в тестовом режиме.** Запуск бота на 1-2 тестовых заказах с минимальным количеством Stars (`--test-order`).

---

## 5. Установка и запуск

### Требования
* Python 3.10+
* `httpx`, `aiosqlite`, `beautifulsoup4`, `pytest`

### Установка зависимостей
```bash
pip install -r requirements.txt
```

### Настройка конфигурации
Скопируйте пример файла переменных окружения:
```bash
cp .env.example .env
```
Заполните обязательные параметры:
```env
FUNPAY_GOLDEN_KEY=ваш_golden_key_из_куки
GAMEAU_API_KEY=ваш_api_ключ_gameau
TELEGRAM_BOT_TOKEN=токен_бота_для_уведомлений
TELEGRAM_CHAT_ID=chat_id_владельца
WHITEBIRD_USDT_RATE=87.63
DEFAULT_MAX_CHARGE_USDT=9.50
```

### Диагностика системы
Проверка подключений к FunPay, Gameau, базы данных и Telegram-бота:
```bash
python main.py --check
```

### Тестовый заказ (Шаг 6)
Проверка отправки минимального пакета Stars на указанный Telegram-аккаунт:
```bash
python main.py --test-order durov --test-qty 50
```

### Основной запуск
```bash
python main.py            # бесконечный асинхронный цикл автовыдачи
python main.py --once     # однократный проход по очереди
python main.py -v         # подробный отладочный лог
```

---

## 6. Запуск тестов

Прогон всех тестов (парсер, база данных, Gameau API, FunPay Long Polling с защитой от 403, Telegram Alert, Order Processor):
```bash
pytest tests/ -v
```
