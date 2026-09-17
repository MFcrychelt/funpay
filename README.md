# AutoStars (FunPay <-> Gameau Engine)

Современный, полностью асинхронный и отказоустойчивый стек автовыдачи **Telegram Stars** на [FunPay](https://funpay.com) с интеграцией B2B API **Gameau** ([документация](https://gameau.us/api-docs.html?section=catalog)) и поддержкой вариантов заведения криптовалюты **USDT TRC-20**.

---

## 1. Архитектурные преимущества новой версии

| Проблема старых версий | Риск / Последствия | Решение в новой архитектуре AutoStars |
| :--- | :--- | :--- |
| **Синхронный `requests` / `time.sleep`** | Пропуск заказов при высокой активности, задержка ответа 10–30 сек. | **`asyncio` + `httpx` (Connection Pool, HTTP/2)** — параллельная неблокирующая обработка. |
| **Отсутствие `Idempotency-Key`** | Сетевой сбой во время запроса = **двойная покупка Stars** и потеря USDT. | **UUID v5/v4 Idempotency Key** на каждый `order_id` в SQLite/`aiosqlite`. |
| **Отсутствие контроля цены (`maxCharge`)** | Поставщик поднял цену = **списание баланса в минус**. | Передача параметра `maxCharge` в Gameau API и превентивная остановка сделки. |
| **Жесткий / ненадежный Regex** | Покупатель написал `t.me/nick` или `@nick_1` — скрипт клинит. | Нормализация юзернеймов с авто-фоллбэком и запросом уточнения в чат FunPay. |
| **Устаревший CSRF на FunPay** | Блокировка за частые запросы страниц или просроченный токен. | Динамическое извлечение `csrf_token` из `data-app-data` + автообновление при `403 Forbidden`. |
| **Жёсткие цены без каталога** | Несоответствие лотов пакетам поставщика. | **Интеграция Gameau Catalog API (`section=catalog`)**: автоподбор пакета и цен. |

---

## 2. Структура проекта

```
autostars/
├── config.py             # Загрузка переменных (golden_key, gameau_key, telegram_bot_token, варианты курсов)
├── database/
│   ├── models.py         # AIOSQLite схемы (orders, idempotency_logs, settings)
│   └── db_manager.py     # Асинхронные методы CRUD
├── clients/
│   ├── funpay.py         # Long Polling runner/, extraction csrf_token, postMessage, 403 recovery
│   └── gameau.py         # B2B API client v1, catalog sync, maxCharge, retry with exponential backoff
├── services/
│   ├── parser.py         # Regex engine для Telegram handles (ссылки, @тэги, фоллбэк)
│   └── order_processor.py# Главная бизнес-логика (Оплата FP -> Parsing -> Gameau -> Ответ FP -> TG Alert)
├── notifier/
│   └── tg_alert.py       # Telegram бот для уведомления владельца (алерты прибыли по 2 вариантам и пополнения)
└── main.py               # Точка входа (Async loop)
```

---

## 3. Финансовая модель и варианты заведения криптовалюты (USDT TRC-20)

Для покупки Telegram Stars через Gameau баланс аккаунта на `gameau.us` должен пополняться в **USDT TRC-20**.
В боте заложена поддержка двух вариантов финансовой модели с автоматическим расчётом чистой прибыли:

### 1️⃣ Вариант 1: Анонимный обмен / P2P (Курс ~110.00 RUB за 1 USDT)
* **Механика:** Покупка USDT TRC-20 через анонимные P2P-площадки, Telegram Wallet (@wallet), Crypto Bot (@send) или обменники BestChange (оплата картой РФ / СБП / наличными) с выводом на TRC-20 адрес из кабинета Gameau.
* **Плюсы:** Полная анонимность, не требуется верификация (KYC).
* **Себестоимость 1 000 Stars (9.10 USDT):** $9.10 \times 110.00 = 1\,001.00$ RUB.
* **Чистая прибыль (при продаже за 1 370.40 ₽):** **+369.40 RUB** (маржинальность 27.0%).

### 2️⃣ Вариант 2: Беларусь Whitebird без P2P (Курс ~87.63 RUB за 1 USDT) [РЕКОМЕНДУЕТСЯ]
* **Механика:** Официальная покупка USDT на лицензированной криптоплатформе **whitebird.io** (резидент ПВТ Беларуси) с банковской карты (BYN / RUB / USD) с прямым выводом на TRC-20 кошелёк Gameau.
* **Плюсы:** Максимальная прибыль, легальный чистый обмен без риска блокировок карт по 115-ФЗ, экономия **+203.57 ₽ с каждого заказа**!
* **Себестоимость 1 000 Stars (9.10 USDT):** $9.10 \times 87.63 = 797.43$ RUB.
* **Чистая прибыль (при продаже за 1 370.40 ₽):** **+572.97 RUB** (маржинальность 41.8%).

### ⚡ Комиссия сети TRON (Energy)
* При выводе с криптобирж/обменников фиксированная комиссия обычно составляет 1–1.5 USDT.
* При отправке с некастодиального кошелька для снижения стоимости комиссии рекомендуется использовать аренду Tron Energy (Feee.io / JustLend) — комиссия падает с 28 TRX до 6–8 TRX.

---

## 4. Интеграция с Gameau Catalog API (`section=catalog`)

Согласно официальной документации [Gameau REST API v1](https://gameau.us/api-docs.html?section=catalog), в бот интегрированы:
* `GET /api/v1/catalog?type=telegramStars` — получение актуального каталога пакетов звёзд, статусов `orderable` и цен.
* Метод `find_stars_package(stars)` — умный подбор пакета: точное совпадение либо ближайший больший пакет с гарантией получения покупателем не меньше оплаченного объёма.
* Контроль `maxCharge` — отправка предельной стоимости с защитой от списания в минус (`409 PRICE_CHANGED`).
* `GET /api/v1/orders/status/{order_id}` — мониторинг статусов выполнения заказа до терминального завершения (`completed`, `delivered`).

---

## 5. Установка и запуск

### Установка зависимостей
```bash
pip install -r requirements.txt
```

### Настройка конфигурации (`.env`)
```bash
cp .env.example .env
```
Заполните параметры:
```env
FUNPAY_GOLDEN_KEY=ваш_golden_key_из_куки
GAMEAU_API_KEY=ваш_api_ключ_gameau
TELEGRAM_BOT_TOKEN=токен_бота_для_уведомлений
TELEGRAM_CHAT_ID=chat_id_владельца

# Выбор активного варианта (1 = Анонимный 110 ₽, 2 = Whitebird 87.63 ₽)
EXCHANGE_VARIANT=2
RATE_VARIANT_1=110.00
RATE_VARIANT_2=87.63
DEFAULT_MAX_CHARGE_USDT=9.50
```

---

## 6. Команды CLI и утилиты

* **Просмотр актуального каталога Gameau с расчетом себестоимости по обоим курсам:**
  ```bash
  python main.py --catalog
  ```

* **Калькулятор чистой прибыли для любой цены лота FunPay:**
  ```bash
  python main.py --calc 1370.40 1000
  ```

* **Инструкция по заводу крипты USDT TRC-20 на Gameau:**
  ```bash
  python main.py --deposit-info
  ```

* **Диагностика всех подключений (FunPay, Gameau, SQLite, Telegram):**
  ```bash
  python main.py --check
  ```

* **Тестовый заказ минимального пакета Stars:**
  ```bash
  python main.py --test-order durov --test-qty 50
  ```

* **Основной асинхронный запуск бота 24/7:**
  ```bash
  python main.py
  ```

---

## 7. Запуск тестов

```bash
pytest tests/ -v
```
Все 83 теста проверяют базу данных, парсер юзернеймов, каталог Gameau, переполучение CSRF при 403, финансовые калькуляции и Telegram алерты.
