# 🚀 Инструкция по установке AutoStars

## Системные требования

- Linux / Windows 10-11 / macOS
- Python 3.10 или выше
- Интернет-соединение
- Аккаунты: FunPay (продавец, golden key), gameau.us (API-ключ),
  Telegram-бот для уведомлений (опционально)

## Установка

```bash
git clone <ваш-репозиторий> && cd funpay

python -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows:
# .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
nano .env   # заполнить FUNPAY_GOLDEN_KEY, GAMEAU_API_KEY, TELEGRAM_*
```

### Где взять ключи

**FUNPAY_GOLDEN_KEY**
1. Войдите в аккаунт FunPay → на любой странице откройте DevTools (F12)
2. Network → Cookies → `golden_key`
3. Вставьте в `.env`

**GAMEAU_API_KEY**
1. gameau.us → Профиль → «API для интеграций» → «Выпустить ключ»
2. Ключ показывается один раз — скопируйте сразу

**TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID**
1. @BotFather → /newbot → токен
2. Напишите боту сообщение; chat_id получите через @userinfobot
   (или `https://api.telegram.org/bot<TOKEN>/getUpdates`)

## Проверка и запуск

```bash
# Диагностика всех модулей (FunPay, GAMEAU, БД, Telegram, курсы)
python -m autostars.main --check

# Каталог GAMEAU с себестоимостью по обоим вариантам курса
python -m autostars.main --catalog

# Тестовый заказ (50 звёзд на ваш аккаунт)
python -m autostars.main --test-order your_username

# Основной цикл автовыдачи
python -m autostars.main
```

## Отчёты и трекер задач

```bash
python -m autostars.main --stats            # 1/2/3/4/6/24ч, день, всего
python -m autostars.main --report           # P&L + убытки + топ + провалы
python -m autostars.main --tasks            # открытые/зависшие задачи + журнал
python -m autostars.main --timeline 123456  # жизненный цикл одной задачи
python -m autostars.main --stats-push       # статистика в Telegram
```

## Docker

```bash
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f
```

База (`autostars.db`) и логи сохраняются в `./data/` на хосте.

## Автозапуск (systemd, Linux)

`/etc/systemd/system/autostars.service`:

```ini
[Unit]
Description=AutoStars - FunPay Stars delivery
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/funpay
ExecStart=/opt/funpay/.venv/bin/python -m autostars.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now autostars
journalctl -u autostars -f
```

## Возможные проблемы

| Симптом | Решение |
|---|---|
| `[ERR] FunPay: ...` в `--check` | неверный `FUNPAY_GOLDEN_KEY`, сессия «offline» в FunPay |
| `401` от GAMEAU | ключ отозван — выпустите новый |
| `LOW_BALANCE` | пополните USDT TRC-20 (инструкция: `--deposit-info`) |
| `PRICE_EXCEEDED` | цена пакета выше `DEFAULT_MAX_CHARGE_USDT` — увеличьте лимит |
| Заказ завис в PROCESSING | бот сам досинхронизирует через reconcile; `--timeline ID` — детали |

## Legacy-компоненты

Исходный релиз AutoStars-New сохранён как отдельный стек:
`python main.py` (консоль, FunPayAPI + db.json), `python gui.py` (Flet-GUI),
пакет `bot/`. Для боевой автовыдачи используйте движок `autostars/`
(`python -m autostars.main`).
