"""Точка входа AutoStars (Async Event Loop).

Запуск:
    python -m autostars.main                     # Асинхронный цикл автовыдачи
    python -m autostars.main --once              # Однократный опрос очереди
    python -m autostars.main --check             # Проверка подключений и конфигурации
    python -m autostars.main --catalog           # Просмотр каталога Gameau с расчетом цен
    python -m autostars.main --calc 1370.4 1000  # Калькулятор прибыли (Вариант 1 vs Вариант 2)
    python -m autostars.main --deposit-info      # Инструкция по заводу крипты USDT TRC-20
    python -m autostars.main --test-order durov  # Тестовый заказ
    python -m autostars.main --stats             # Статистика за 1/2/3/4/6/24ч, день и всего
    python -m autostars.main --report            # Полный отчёт: P&L, убытки, топ, трекинг задач
    python -m autostars.main --tasks             # Трекинг выполнения задач (открытые/зависшие/журнал)
    python -m autostars.main --timeline 123456   # Полный жизненный цикл конкретной задачи
    python -m autostars.main --stats-push        # Отправить текущую статистику в Telegram
    python -m autostars.main --balance           # Баланс GAMEAU и на сколько заказов его хватит
    python -m autostars.main --pause / --resume  # Пауза/возобновление приёма новых заказов
    python -m autostars.main --retry-order 123   # Повтор проваленного заказа (идемпотентно)

В живом цикле дополнительно доступны TG-команды (/help):
/stats /report /tasks /balance /pause /resume /retry <id> /calc <цена> [звёзды] /status
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import List, Optional

from .config import Config
from .database.db_manager import DBManager
from .clients.funpay import FunPayClient
from .clients.gameau import GameauClient
from .notifier.tg_alert import TelegramNotifier
from .notifier.tg_commands import TelegramCommandServer, build_default_handlers
from .services import bot_control
from .services.order_processor import process_paid_order
from .services.statistics import StatisticsService
from .services.task_tracker import TaskTracker, reconcile_inflight_orders

logger = logging.getLogger("autostars")


def setup_logging(verbose: bool = False, cfg: Optional[Config] = None) -> None:
    """Настройка логирования: консоль + ротируемый файл (по умолчанию autostars.log)."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = cfg.log_file if cfg else None
    if log_file:
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=cfg.log_max_bytes if cfg else 5_000_000,
                backupCount=cfg.log_backup_count if cfg else 5,
                encoding="utf-8",
            )
        )

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


# ============================================================================ #
# Статистика и отчёты (трекинг задач, убытки/прибыль/затраты)
# ============================================================================ #

def _build_statistics(db: DBManager, cfg: Config) -> StatisticsService:
    return StatisticsService(
        db=db,
        rate_variant_1=cfg.rate_variant_1,
        rate_variant_2=cfg.rate_variant_2,
        active_variant=cfg.exchange_variant,
        tron_energy_fee_rub=cfg.tron_energy_fee_rub,
    )


async def cmd_stats(cfg: Config, hours: Optional[List[float]] = None) -> int:
    """Вывод статистики по окнам: 1ч/2ч/3ч/4ч/6ч/24ч + календарный день + всего."""
    db = DBManager(cfg.db_path)
    await db.init_db()
    try:
        stats = _build_statistics(db, cfg)
        if hours:
            windows = [await stats.window(f"последние {h:g} ч", h) for h in hours]
            windows.append(await stats.today_window())
        else:
            windows = await stats.compute_all()
        print(stats.render_text(windows))
        return 0
    finally:
        await db.close()


async def cmd_report(cfg: Config) -> int:
    """Полный отчёт: статистика + P&L + убытки + топ сделок + провалы + трекинг."""
    db = DBManager(cfg.db_path)
    await db.init_db()
    try:
        stats = _build_statistics(db, cfg)
        windows = await stats.compute_all()

        now = int(time.time())
        day_ago = now - 86400
        top = await db.top_orders(limit=5, start_ts=day_ago, end_ts=now)
        failed = await db.top_orders(limit=10, start_ts=day_ago, end_ts=now, failed_only=True)
        negative = await db.get_negative_profit_orders(limit=10, start_ts=day_ago, end_ts=now)
        status_counts = await db.get_status_counts()

        report = stats.render_full_report(windows, top, failed, status_counts)
        if negative:
            report += "\n🔻 Убыточные сделки за 24ч (маржа < 0):\n"
            for o in negative:
                report += (
                    f"   #{o['order_id']} @{o.get('username') or '?'} — "
                    f"выручка {float(o.get('price_rub') or 0):.2f} ₽, "
                    f"затраты {float(o.get('cost_usdt') or 0):.2f} USDT → "
                    f"убыток {float(o.get('profit_rub') or 0):.2f} ₽\n"
                )
            report += "\n   → проверьте цену лота на FunPay или курс закупки USDT\n"
        print(report)
        return 0
    finally:
        await db.close()


async def cmd_tasks(cfg: Config) -> int:
    """Трекинг выполнения задач."""
    db = DBManager(cfg.db_path)
    await db.init_db()
    try:
        tracker = TaskTracker(db)
        print(await tracker.render_text(stuck_minutes=cfg.stuck_task_minutes))
        return 0
    finally:
        await db.close()


async def cmd_timeline(cfg: Config, order_id: str) -> int:
    """Полный жизненный цикл задачи по ID заказа."""
    db = DBManager(cfg.db_path)
    await db.init_db()
    try:
        tracker = TaskTracker(db)
        order = await db.get_order(order_id)
        if order is None:
            print(f"Заказ #{order_id} не найден в базе.")
            return 1
        print(f"\n📋 ЗАДАЧА #{order['order_id']} — {order['status']}")
        print(f"   Получатель: @{order.get('username') or 'не указан'} | "
              f"Звёзды: {order.get('quantity')} | Цена: {order.get('price_rub')} ₽ | "
              f"Затраты: {order.get('cost_usdt')} USDT | Прибыль: {order.get('profit_rub')} ₽")
        print(f"   GAMEAU ID: {order.get('gameau_order_id') or '—'} | "
              f"Ошибка: {order.get('error') or '—'}")
        print(f"   Создан: {order.get('created_at')} | Обновлён: {order.get('updated_at')}\n")
        events = await tracker.timeline(order_id)
        if not events:
            print("   Событий в журнале нет (заказ создан до v2.1).")
        else:
            print("Журнал выполнения:")
            for e in events:
                ts = time.strftime("%d.%m %H:%M:%S", time.localtime(e.get("ts_epoch") or 0))
                print(f"   {ts} | {e['event']:<18} | {e.get('detail') or ''}")
        print()
        return 0
    finally:
        await db.close()


async def cmd_balance(cfg: Config) -> int:
    """Текущий баланс GAMEAU."""
    gameau = GameauClient(api_key=cfg.gameau_key, base_url=cfg.gameau_base_url)
    notifier = TelegramNotifier(
        bot_token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        rate_variant_1=cfg.rate_variant_1,
        rate_variant_2=cfg.rate_variant_2,
        active_variant=cfg.exchange_variant,
    )
    try:
        info = await bot_control.check_gameau_balance(
            gameau, notifier, cfg.low_balance_threshold_usdt, alert=False
        )
        if info is None:
            print("[ERR] Не удалось получить баланс GAMEAU (проверьте API-ключ и сеть).")
            return 1
        emoji = "🚨" if info["balance"] < cfg.low_balance_threshold_usdt else "💰"
        print(f"{emoji} Баланс GAMEAU: {info['balance']:.2f} {info['currency']} "
              f"(тариф {info['plan']}, статус {info['status']})")
        print(f"   Порог алерта: {cfg.low_balance_threshold_usdt:.2f} {info['currency']}")
        cost_1000 = 9.10 * cfg.active_usdt_rate
        can_pay = int(info["balance"] // 9.10) if info["balance"] > 0 else 0
        print(f"   Хватает на: ~{can_pay} заказ(ов) на 1000 Stars "
              f"(9.10 USDT, себестоимость ~{cost_1000:.2f} ₽ по активному курсу)")
        return 0
    finally:
        await gameau.close()
        await notifier.close()


async def cmd_pause(cfg: Config, pause: bool) -> int:
    """Поставить/снять паузу приёма новых заказов (переживает рестарт)."""
    db = DBManager(cfg.db_path)
    await db.init_db()
    try:
        await bot_control.set_bot_paused(db, pause)
        print("⏸ ПРИЁМ НОВЫХ ЗАКАЗОВ ОСТАНОВЛЕН. В работе — принятые заказы и reconciliation."
              if pause else
              "🟢 ВЫДАЧА ВОЗОБНОВЛЕНА. Новые заказы FunPay снова обрабатываются.")
        return 0
    finally:
        await db.close()


async def cmd_retry(cfg: Config, order_id: str) -> int:
    """Повторить проваленный заказ (идемпотентно, по Idempotency-Key)."""
    cfg.validate()
    db = DBManager(cfg.db_path)
    await db.init_db()

    funpay = FunPayClient(golden_key=cfg.golden_key, user_agent=cfg.funpay_user_agent)
    gameau = GameauClient(api_key=cfg.gameau_key, base_url=cfg.gameau_base_url)
    notifier = TelegramNotifier(
        bot_token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        rate_variant_1=cfg.rate_variant_1,
        rate_variant_2=cfg.rate_variant_2,
        active_variant=cfg.exchange_variant,
        tron_energy_fee_rub=cfg.tron_energy_fee_rub,
    )
    tracker = TaskTracker(db)

    try:
        row = await db.get_order(order_id)
        if row is None:
            print(f"[ERR] Заказ #{order_id} не найден в базе.")
            return 1
        print(f"Ретрай заказа #{order_id} (текущий статус: {row.get('status')}, "
              f"указанная цена: {row.get('price_rub')} ₽)")
        result = await bot_control.retry_failed_order(
            order_id=order_id,
            db=db,
            funpay_client=funpay,
            gameau_client=gameau,
            tg_notifier=notifier,
            tracker=tracker,
            default_quantity=cfg.default_stars_quantity,
            max_charge_usdt=cfg.default_max_charge_usdt,
            whitebird_rate=cfg.active_usdt_rate,
            hide_sender=cfg.hide_sender,
            max_order_retries=cfg.max_order_retries,
            wait_completion_timeout=cfg.wait_completion_timeout,
        )
        print(f"Результат: {result}")
        updated = await db.get_order(order_id)
        if updated:
            print(f"Новый статус: {updated['status']} "
                  f"(затраты {updated.get('cost_usdt')} USDT, прибыль {updated.get('profit_rub')} ₽)")
        return 0 if result.get("status") in ("completed", "unconfirmed") else 1
    finally:
        await funpay.close()
        await gameau.close()
        await notifier.close()
        await db.close()


async def cmd_stats_push(cfg: Config) -> int:
    """Отправка текущего отчёта в Telegram."""
    db = DBManager(cfg.db_path)
    await db.init_db()
    try:
        stats = _build_statistics(db, cfg)
        windows = await stats.compute_all()
        text = stats.render_telegram(windows)

        notifier = TelegramNotifier(
            bot_token=cfg.telegram_bot_token,
            chat_id=cfg.telegram_chat_id,
            rate_variant_1=cfg.rate_variant_1,
            rate_variant_2=cfg.rate_variant_2,
            active_variant=cfg.exchange_variant,
        )
        if not notifier.is_configured:
            print("[WARN] Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Отчёт:")
            print(text)
            return 1
        ok = await notifier.send_stats_report(text)
        print("✅ Отчёт отправлен в Telegram." if ok else "❌ Не удалось отправить отчёт в Telegram.")
        return 0 if ok else 1
    finally:
        await notifier.close()
        await db.close()


# ============================================================================ #
# Диагностика и каталог
# ============================================================================ #

async def check_system(cfg: Config) -> bool:
    """Диагностика всех модулей системы (FunPay, Gameau, DB, Telegram)."""
    print("\n🔍 === ДИАГНОСТИКА СИСТЕМЫ AUTOSTARS ===")
    all_ok = True

    # 1. База данных
    db = DBManager(cfg.db_path)
    try:
        await db.init_db()
        print(f"[OK] База данных SQLite подключена: {cfg.db_path}")
    except Exception as exc:
        print(f"[ERR] Ошибка SQLite: {exc}")
        all_ok = False
    finally:
        await db.close()

    # 2. FunPay Client
    funpay = FunPayClient(golden_key=cfg.golden_key, user_agent=cfg.funpay_user_agent)
    try:
        csrf = await funpay.refresh_csrf()
        print(
            f"[OK] FunPay авторизован: user_id={funpay.user_id}, "
            f"аккаунт={funpay.username or 'не определен'}, csrf={csrf[:8]}..."
        )
    except Exception as exc:
        print(f"[ERR] FunPay: {exc}")
        all_ok = False
    finally:
        await funpay.close()

    # 3. Gameau Client
    gameau = GameauClient(api_key=cfg.gameau_key, base_url=cfg.gameau_base_url)
    try:
        acc = await gameau.get_account()
        if "error" not in acc:
            balance = acc.get("balance", "N/A")
            currency = acc.get("currency", "USD")
            plan = acc.get("plan", "Standard")
            print(f"[OK] Gameau API доступен. Баланс: {balance} {currency} (Тариф: {plan})")
            try:
                bal = float(balance)
                if bal < cfg.low_balance_threshold_usdt:
                    print(f"[WARN] Баланс ниже порога {cfg.low_balance_threshold_usdt:.2f} — "
                          f"риск остановки выдачи (LOW_BALANCE)")
            except (TypeError, ValueError):
                pass
        else:
            print(f"[WARN] Gameau API вернул ответ: {acc}")
    except Exception as exc:
        print(f"[ERR] Gameau: {exc}")
        all_ok = False
    finally:
        await gameau.close()

    # 4. Telegram Notifier
    notifier = TelegramNotifier(
        bot_token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        rate_variant_1=cfg.rate_variant_1,
        rate_variant_2=cfg.rate_variant_2,
        active_variant=cfg.exchange_variant,
    )
    if notifier.is_configured:
        print(f"[OK] Telegram Alert настроен (chat_id={cfg.telegram_chat_id})")
    else:
        print("[INFO] Telegram Alert не настроен (опционально)")

    print(f"\n📊 Финансовые модели пополнения USDT TRC-20:")
    print(f"• Вариант 1 (Анонимный обмен / P2P): {cfg.rate_variant_1:.2f} RUB/USDT (1000 Stars ≈ {9.10 * cfg.rate_variant_1:.2f} ₽)")
    print(f"• Вариант 2 (Беларусь Whitebird): {cfg.rate_variant_2:.2f} RUB/USDT (1000 Stars ≈ {9.10 * cfg.rate_variant_2:.2f} ₽)")
    print(f"• Активный вариант: Вариант {cfg.exchange_variant} (курс {cfg.active_usdt_rate:.2f} RUB/USDT)")
    print(f"🛡️ Лимит maxCharge: {cfg.default_max_charge_usdt} USDT")
    print(f"🗂 Трекинг задач: зависание > {cfg.stuck_task_minutes} мин, "
          f"согласование с GAMEAU каждые {cfg.reconciliation_interval_sec}s, "
          f"повторы при сбоях: {cfg.max_order_retries}")
    print(f"💬 Чат-мониторинг WAITING_USERNAME каждые {cfg.chat_monitor_interval_sec}s "
          f"(подхватывает @username из чата)")
    print(f"💰 Контроль баланса GAMEAU: порог {cfg.low_balance_threshold_usdt:.2f} USDT, "
          f"проверка каждые {cfg.balance_check_interval_min} мин")
    if cfg.stats_push_interval_min > 0:
        print(f"📊 Авто-отправка статистики в Telegram каждые {cfg.stats_push_interval_min} мин")
    else:
        print("📊 Авто-отправка статистики в Telegram: выключена (команда --stats)")
    print("=========================================\n")
    return all_ok


async def show_catalog(cfg: Config, item_type: str = "telegramStars") -> None:
    """Выводит актуальный каталог Gameau с ценами и себестоимостью по двум вариантам курса."""
    gameau = GameauClient(api_key=cfg.gameau_key, base_url=cfg.gameau_base_url)
    print(f"\n📦 === КАТАЛОГ GAMEAU ({item_type}) ===")
    try:
        items = await gameau.get_catalog(item_type=item_type)
        if not items:
            print("Каталог пуст или недоступен (проверьте API-ключ).")
            return

        print(f"{'Название / Пакет':<30} | {'Цена (USDT)':<12} | {'Вариант 2 (87.63 ₽)':<20} | {'Вариант 1 (110 ₽)':<18}")
        print("-" * 88)

        for item in items:
            name = item.get("name") or item.get("title") or "Unknown"
            price = float(item.get("price") or 0.0)
            cost_v2 = round(price * cfg.rate_variant_2, 2)
            cost_v1 = round(price * cfg.rate_variant_1, 2)
            print(f"{name:<30} | {price:<12.2f} | {cost_v2:<17.2f} ₽ | {cost_v1:<15.2f} ₽")

        print("=" * 88)
        print("💡 Цены указаны по вашему тарифу на Gameau с учетом обоих вариантов обмена.")
    except Exception as exc:
        print(f"[ERR] Ошибка при загрузке каталога: {exc}")
    finally:
        await gameau.close()


# ============================================================================ #
# Калькулятор и завод крипты
# ============================================================================ #

def print_deposit_info(cfg: Config) -> None:
    """Инструкция по заводу криптовалюты USDT TRC-20 на сайт gameau.us."""
    print("\n💎 === РУКОВОДСТВО ПО ЗАВОДУ КРИПТЫ USDT TRC-20 НА GAMEAU.US ===")
    print(f"""
Для автоматической покупки Telegram Stars бот списывает USDT с баланса Gameau.
В кабинете gameau.us перейдите: Профиль -> Пополнить баланс -> USDT TRC-20.
Скопируйте ваш депозитный адрес кошелька в сети TRON (начинается на 'T...').

--------------------------------------------------------------------------------
1️⃣ ВАРИАНТ 1: АНОНИМНЫЙ ОБМЕН / P2P (Курс ~{cfg.rate_variant_1:.2f} RUB за 1 USDT)
--------------------------------------------------------------------------------
• Плюсы: Полная анонимность, не требует верификации по паспорту.
• Минусы: Высокий курс (переплата ~{cfg.rate_variant_1 - cfg.rate_variant_2:.2f} ₽ на каждый USDT), риски P2P блокировок карт.
• Способы покупки:
    1. Telegram Wallet P2P (@wallet) / Telegram Crypto Bot (@send).
    2. Агрегаторы BestChange (направление: Сбербанк/Тинькофф/СБП -> Tether TRC20).
    3. Криптоматы / Cash-in наличными.
• Расчет:
    - 1 000 Stars (9.10 USDT) = {9.10 * cfg.rate_variant_1:.2f} RUB себестоимость.
    - При продаже за 1 370.40 RUB чистая прибыль = +{1370.40 - 9.10 * cfg.rate_variant_1:.2f} RUB.

--------------------------------------------------------------------------------
2️⃣ ВАРИАНТ 2: БЕЛАРУСЬ WHITEBIRD БЕЗ P2P (Курс ~{cfg.rate_variant_2:.2f} RUB за 1 USDT) [РЕКОМЕНДУЕТСЯ]
--------------------------------------------------------------------------------
• Плюсы: Максимальная прибыль (+{9.10 * (cfg.rate_variant_1 - cfg.rate_variant_2):.2f} ₽ экономии на каждом заказе!),
         официальный легальный обменник (ПВТ Беларусь), нет блокировок 115-ФЗ.
• Минусы: Требуется разовая быстрая верификация (KYC).
• Инструкция:
    1. Зайдите на официальный сайт https://whitebird.io
    2. Выберите покупку USDT (TRC-20) с банковской карты (RUB / BYN / USD).
    3. В поле адреса получателя укажите адрес TRC-20 из личного кабинета gameau.us.
    4. Оплатите заказ — USDT моментально поступают на баланс Gameau без посредников!
• Расчет:
    - 1 000 Stars (9.10 USDT) = {9.10 * cfg.rate_variant_2:.2f} RUB себестоимость.
    - При продаже за 1 370.40 RUB чистая прибыль = +{1370.40 - 9.10 * cfg.rate_variant_2:.2f} RUB.

--------------------------------------------------------------------------------
⚡ КОМИССИЯ СЕТИ TRON (ENERGY):
• При прямом выводе с бирж/Whitebird комиссия вывода обычно составляет 1-1.5 USDT.
• При частых переводах с личного кошелька используйте аренду Tron Energy
  (Feee.io / JustLend) для снижения комиссии за транзакцию с 28 TRX до 6-8 TRX.
================================================================================
""")


def print_profit_calc(cfg: Config, price_rub: float, stars: int = 1000) -> None:
    """Калькулятор чистой прибыли."""
    notifier = TelegramNotifier(
        rate_variant_1=cfg.rate_variant_1,
        rate_variant_2=cfg.rate_variant_2,
        active_variant=cfg.exchange_variant,
    )
    # 1000 stars = 9.10 USDT, пропорционально для другого числа
    usdt_cost = round((stars / 1000.0) * 9.10, 2)
    summary = notifier.get_profit_summary(price_rub, usdt_cost)

    print(f"\n📊 === КАЛЬКУЛЯТОР ПРИБЫЛИ ДЛЯ {stars} STARS ===")
    print(f"Выручка на FunPay: {price_rub:.2f} RUB | Затраты на Gameau: {usdt_cost:.2f} USDT")
    print("-" * 65)
    print(f"• Вариант 2 (Whitebird {cfg.rate_variant_2:.2f} ₽):")
    print(f"   Себестоимость: {summary['variant_2']['cost_rub']:.2f} RUB")
    print(f"   Чистая прибыль: +{summary['variant_2']['profit_rub']:.2f} RUB 💰 (Маржинальность: {summary['variant_2']['profit_rub']/price_rub*100:.1f}%)")
    print(f"• Вариант 1 (Анонимный {cfg.rate_variant_1:.2f} ₽):")
    print(f"   Себестоимость: {summary['variant_1']['cost_rub']:.2f} RUB")
    print(f"   Чистая прибыль: +{summary['variant_1']['profit_rub']:.2f} RUB (Маржинальность: {summary['variant_1']['profit_rub']/price_rub*100:.1f}%)")
    diff = summary['variant_2']['profit_rub'] - summary['variant_1']['profit_rub']
    print("-" * 65)
    print(f"🚀 Экономия при использовании Whitebird: +{diff:.2f} RUB с каждой сделки!")
    print("=" * 65 + "\n")


# ============================================================================ #
# Тестовый заказ
# ============================================================================ #

async def run_test_order(
    cfg: Config,
    username: str,
    quantity: int = 50,
) -> None:
    """Выполняет тестовый заказ для проверки интеграции."""
    print(f"\n🚀 Запуск тестового заказа: {quantity} Stars для @{username}...")
    db = DBManager(cfg.db_path)
    await db.init_db()

    funpay = FunPayClient(golden_key=cfg.golden_key, user_agent=cfg.funpay_user_agent)
    gameau = GameauClient(api_key=cfg.gameau_key, base_url=cfg.gameau_base_url)
    notifier = TelegramNotifier(
        bot_token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        rate_variant_1=cfg.rate_variant_1,
        rate_variant_2=cfg.rate_variant_2,
        active_variant=cfg.exchange_variant,
        tron_energy_fee_rub=cfg.tron_energy_fee_rub,
    )
    tracker = TaskTracker(db)

    test_order_data = {
        "id": f"TEST_{int(time.time())}",
        "chat_node": "test_chat",
        "last_message": f"@{username}",
        "description": f"Тестовый заказ {quantity} Stars",
        "quantity": quantity,
        "price": 100.0,
    }

    try:
        result = await process_paid_order(
            order_data=test_order_data,
            funpay_client=funpay,
            gameau_client=gameau,
            db=db,
            tg_notifier=notifier,
            default_quantity=quantity,
            max_charge_usdt=cfg.default_max_charge_usdt,
            whitebird_rate=cfg.active_usdt_rate,
            hide_sender=cfg.hide_sender,
            task_tracker=tracker,
            max_order_retries=cfg.max_order_retries,
            wait_completion_timeout=cfg.wait_completion_timeout,
        )
        print(f"Результат тестового заказа: {result}")
    finally:
        await funpay.close()
        await gameau.close()
        await notifier.close()
        await db.close()


# ============================================================================ #
# Основной цикл автовыдачи
# ============================================================================ #

async def _push_periodic_stats(
    cfg: Config,
    db: DBManager,
    notifier: TelegramNotifier,
    stats: StatisticsService,
) -> None:
    """Отправка периодической сводки (1 час + 24 часа + сегодня) в Telegram."""
    windows = [
        await stats.window("1 час", 1),
        await stats.day_window(),
        await stats.today_window(),
        await stats.total_window(),
    ]
    text = stats.render_telegram(windows)
    if await notifier.send_stats_report(text):
        logger.info("Периодическая статистика отправлена в Telegram")


async def run_bot(cfg: Config, once: bool = False) -> None:
    """Главный асинхронный цикл обработки заказов."""
    cfg.validate()

    db = DBManager(cfg.db_path)
    await db.init_db()

    # Пауза, поставленная /pause или --pause, переживает перезапуск
    if await bot_control.is_bot_paused(db):
        logger.warning("Бот запущен в режиме ПАУЗЫ (новые заказы не принимаются). "
                       "Снять: python -m autostars.main --resume или /resume в TG.")

    funpay = FunPayClient(
        golden_key=cfg.golden_key,
        user_agent=cfg.funpay_user_agent,
        base_url=cfg.funpay_base_url,
    )
    gameau = GameauClient(
        api_key=cfg.gameau_key,
        base_url=cfg.gameau_base_url,
        max_retries=cfg.gameau_max_retries,
        timeout=cfg.gameau_timeout,
    )
    notifier = TelegramNotifier(
        bot_token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        rate_variant_1=cfg.rate_variant_1,
        rate_variant_2=cfg.rate_variant_2,
        active_variant=cfg.exchange_variant,
        tron_energy_fee_rub=cfg.tron_energy_fee_rub,
    )
    tracker = TaskTracker(db)
    stats = _build_statistics(db, cfg)

    logger.info("Инициализация сессии FunPay...")
    await funpay.login()
    logger.info("Сессия FunPay активна. Запуск Long Polling / очереди заказов.")

    # Статус-алерт владельцу о старте
    if notifier.is_configured:
        await notifier.send_alert(
            "🤖 <b>AUTOSTARS запущен</b>\n"
            f"Курс USDT: Вариант {cfg.exchange_variant} = {cfg.active_usdt_rate:.2f} ₽\n"
            f"maxCharge: {cfg.default_max_charge_usdt} USDT | "
            f"Polling: {cfg.poll_interval:g}s | "
            f"Reconcile: {cfg.reconciliation_interval_sec}s | "
            f"ChatMonitor: {cfg.chat_monitor_interval_sec}s"
        )

    # Баланс GAMEAU при старте (алерт, если ниже порога)
    try:
        balance = await bot_control.check_gameau_balance(
            gameau, notifier, cfg.low_balance_threshold_usdt, alert=True
        )
        if balance:
            logger.info(f"Баланс GAMEAU при старте: {balance['balance']:.2f} {balance['currency']}")
    except Exception as exc:
        logger.warning(f"Не удалось проверить баланс GAMEAU при старте: {exc}")

    tasks: set[asyncio.Task] = set()
    last_reconcile = 0.0
    last_stats_push = 0.0
    last_chat_monitor = 0.0
    last_balance_check = time.time()
    stats_push_sec = cfg.stats_push_interval_min * 60
    balance_check_sec = cfg.balance_check_interval_min * 60

    # Интерактивный TG-бот (команды /stats /pause /retry ...)
    tg_stop = asyncio.Event()
    tg_server = None
    tg_task: Optional[asyncio.Task] = None
    if notifier.is_configured:
        tg_server = TelegramCommandServer(
            notifier=notifier,
            handlers=build_default_handlers(
                cfg=cfg,
                db=db,
                gameau_client=gameau,
                notifier=notifier,
                stats=stats,
                tracker=tracker,
                loop_controls={
                    "funpay": funpay,
                    "gameau": gameau,
                    "set_paused": lambda p: bot_control.set_bot_paused(db, p),
                },
            ),
        )
        tg_task = asyncio.create_task(tg_server.run(tg_stop), name="tg-commands")
        logger.info("Интерактивный TG-бот: запущен (/help — список команд)")

    try:
        while True:
            try:
                now = time.time()
                paused = await bot_control.is_bot_paused(db)

                if not paused:
                    # Опрашиваем оплаченные заказы
                    paid_orders = await funpay.get_paid_orders()
                    if paid_orders:
                        logger.info(f"Найдено оплаченных заказов: {len(paid_orders)}")
                        for order in paid_orders:
                            order_id = str(order["id"])
                            if await db.is_order_processed(order_id):
                                continue

                            # Асинхронная неблокирующая обработка каждого заказа
                            task = asyncio.create_task(
                                process_paid_order(
                                    order_data=order,
                                    funpay_client=funpay,
                                    gameau_client=gameau,
                                    db=db,
                                    tg_notifier=notifier,
                                    default_quantity=cfg.default_stars_quantity,
                                    max_charge_usdt=cfg.default_max_charge_usdt,
                                    whitebird_rate=cfg.active_usdt_rate,
                                    hide_sender=cfg.hide_sender,
                                    task_tracker=tracker,
                                    max_order_retries=cfg.max_order_retries,
                                    wait_completion_timeout=cfg.wait_completion_timeout,
                                )
                            )
                            tasks.add(task)
                            task.add_done_callback(tasks.discard)

                    # Чат-мониторинг: подхватываем @username, который покупатель
                    # написал в чате (заказы WAITING_USERNAME)
                    if now - last_chat_monitor >= cfg.chat_monitor_interval_sec:
                        last_chat_monitor = now
                        try:
                            await bot_control.process_waiting_username_orders(
                                db=db,
                                funpay_client=funpay,
                                gameau_client=gameau,
                                tg_notifier=notifier,
                                tracker=tracker,
                                default_quantity=cfg.default_stars_quantity,
                                max_charge_usdt=cfg.default_max_charge_usdt,
                                whitebird_rate=cfg.active_usdt_rate,
                                hide_sender=cfg.hide_sender,
                                max_order_retries=cfg.max_order_retries,
                                wait_completion_timeout=cfg.wait_completion_timeout,
                            )
                        except Exception as exc:
                            logger.error(f"Ошибка чат-мониторинга: {exc}", exc_info=True)
                else:
                    logger.debug("Бот в паузе — приём новых заказов остановлен")

                if once:
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    logger.info("Однократный проход завершен.")
                    break

                # Long Polling runner/
                await funpay.poll_runner()

                # Авто-согласование незавершённых задач с GAMEAU
                if now - last_reconcile >= cfg.reconciliation_interval_sec:
                    last_reconcile = now
                    try:
                        await reconcile_inflight_orders(
                            tracker=tracker,
                            db=db,
                            gameau_client=gameau,
                            funpay_client=funpay,
                            tg_notifier=notifier,
                            stuck_minutes=cfg.stuck_task_minutes,
                        )
                    except Exception as exc:
                        logger.error(f"Ошибка reconciliation: {exc}", exc_info=True)

                # Периодическая отправка статистики в Telegram
                if stats_push_sec > 0 and now - last_stats_push >= stats_push_sec:
                    last_stats_push = now
                    try:
                        await _push_periodic_stats(cfg, db, notifier, stats)
                    except Exception as exc:
                        logger.error(f"Ошибка отправки периодической статистики: {exc}", exc_info=True)

                # Периодическая проверка баланса GAMEAU
                if balance_check_sec > 0 and now - last_balance_check >= balance_check_sec:
                    last_balance_check = now
                    try:
                        await bot_control.check_gameau_balance(
                            gameau, notifier, cfg.low_balance_threshold_usdt, alert=True
                        )
                    except Exception as exc:
                        logger.error(f"Ошибка проверки баланса: {exc}", exc_info=True)

            except Exception as exc:
                logger.error(f"Ошибка в цикле автовыдачи: {exc}", exc_info=True)

            await asyncio.sleep(cfg.poll_interval)

    finally:
        tg_stop.set()
        if tg_task is not None:
            tg_task.cancel()
            try:
                await tg_task
            except (asyncio.CancelledError, Exception):
                pass
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if notifier.is_configured:
            try:
                await notifier.send_alert("🛑 <b>AUTOSTARS остановлен</b>")
            except Exception:
                pass
        await funpay.close()
        await gameau.close()
        if tg_server is not None:
            await tg_server.close()
        await notifier.close()
        await db.close()


def main() -> None:
    """Точка входа CLI."""
    parser = argparse.ArgumentParser(
        description="AutoStars: асинхронная автовыдача Telegram Stars на FunPay через Gameau B2B API."
    )
    parser.add_argument("--once", action="store_true", help="однократный проход по очереди")
    parser.add_argument("--check", action="store_true", help="диагностика подключений и конфигурации")
    parser.add_argument("--catalog", action="store_true", help="показать актуальный каталог Gameau (section=catalog)")
    parser.add_argument("--deposit-info", action="store_true", help="инструкция по заводу крипты USDT TRC-20 (Вариант 1 vs 2)")
    parser.add_argument(
        "--calc",
        nargs="+",
        metavar=("PRICE_RUB", "STARS"),
        help="калькулятор чистой прибыли: --calc 1370.40 [1000]",
    )
    parser.add_argument("--stats", action="store_true", help="статистика за 1/2/3/4/6/24ч, календарный день и всего")
    parser.add_argument(
        "--stats-hours",
        default=None,
        help="свой набор часовых окон для --stats, через запятую (напр. 1,6,24)",
    )
    parser.add_argument("--report", action="store_true", help="полный отчёт: P&L, убытки, топ сделок, трекинг задач")
    parser.add_argument("--tasks", action="store_true", help="трекинг выполнения задач (открытые/зависшие/журнал)")
    parser.add_argument(
        "--timeline",
        metavar="ORDER_ID",
        help="полный жизненный цикл задачи по ID заказа FunPay",
    )
    parser.add_argument("--stats-push", action="store_true", help="отправить текущую статистику в Telegram")
    parser.add_argument("--balance", action="store_true", help="баланс GAMEAU (сколько заказов ещё осилить)")
    parser.add_argument("--pause", action="store_true", help="остановить приём новых заказов (переживает рестарт)")
    parser.add_argument("--resume", action="store_true", help="возобновить приём новых заказов")
    parser.add_argument(
        "--retry-order",
        metavar="ORDER_ID",
        help="повторить проваленный заказ (идемпотентно: без дубля покупки)",
    )
    parser.add_argument("--config", default="config.json", help="путь к файлу конфигурации")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный отладочный вывод")
    parser.add_argument(
        "--test-order",
        metavar="USERNAME",
        help="выполнить тестовый заказ на указанный username (например: --test-order durov)",
    )
    parser.add_argument(
        "--test-qty",
        type=int,
        default=50,
        help="количество Stars для тестового заказа (по умолчанию 50)",
    )

    args = parser.parse_args()
    cfg = Config.load(args.config)
    setup_logging(verbose=args.verbose, cfg=cfg)

    if args.deposit_info:
        print_deposit_info(cfg)
        sys.exit(0)

    if args.calc:
        try:
            price = float(args.calc[0])
            stars = int(args.calc[1]) if len(args.calc) > 1 else 1000
            print_profit_calc(cfg, price, stars)
            sys.exit(0)
        except ValueError:
            print("Использование: --calc СУММА_В_РУБ [КОЛИЧЕСТВО_ЗВЁЗД] (например: --calc 1370.4 1000)")
            sys.exit(1)

    if args.catalog:
        asyncio.run(show_catalog(cfg))
        sys.exit(0)

    if args.check:
        success = asyncio.run(check_system(cfg))
        sys.exit(0 if success else 1)

    if args.stats:
        hours: Optional[List[float]] = None
        if args.stats_hours:
            try:
                hours = [float(h) for h in str(args.stats_hours).split(",") if h.strip()]
            except ValueError:
                print("Окна указываются через запятую: --stats-hours 1,6,24")
                sys.exit(1)
        sys.exit(asyncio.run(cmd_stats(cfg, hours)))

    if args.report:
        sys.exit(asyncio.run(cmd_report(cfg)))

    if args.tasks:
        sys.exit(asyncio.run(cmd_tasks(cfg)))

    if args.timeline:
        sys.exit(asyncio.run(cmd_timeline(cfg, args.timeline)))

    if args.stats_push:
        sys.exit(asyncio.run(cmd_stats_push(cfg)))

    if args.balance:
        sys.exit(asyncio.run(cmd_balance(cfg)))

    if args.pause:
        sys.exit(asyncio.run(cmd_pause(cfg, pause=True)))

    if args.resume:
        sys.exit(asyncio.run(cmd_pause(cfg, pause=False)))

    if args.retry_order:
        try:
            cfg.validate()
        except ValueError as exc:
            print(f"[ERR] {exc}")
            sys.exit(1)
        sys.exit(asyncio.run(cmd_retry(cfg, args.retry_order)))

    if args.test_order:
        try:
            cfg.validate()
        except ValueError as exc:
            print(f"[ERR] {exc}")
            sys.exit(1)
        asyncio.run(run_test_order(cfg, args.test_order, args.test_qty))
        sys.exit(0)

    try:
        asyncio.run(run_bot(cfg, once=args.once))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Работа бота остановлена пользователем.")


if __name__ == "__main__":
    main()
