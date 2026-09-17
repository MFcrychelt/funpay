"""Точка входа AutoStars (Async Event Loop).

Запуск:
    python -m autostars.main                     # Асинхронный цикл автовыдачи
    python -m autostars.main --once              # Однократный опрос очереди
    python -m autostars.main --check             # Проверка подключений и конфигурации
    python -m autostars.main --catalog           # Просмотр каталога Gameau с расчетом цен
    python -m autostars.main --calc 1370.4 1000  # Калькулятор прибыли (Вариант 1 vs Вариант 2)
    python -m autostars.main --deposit-info      # Инструкция по заводу крипты USDT TRC-20
    python -m autostars.main --test-order durov  # Тестовый заказ
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Optional

from .config import Config
from .database.db_manager import DBManager
from .clients.funpay import FunPayClient
from .clients.gameau import GameauClient
from .notifier.tg_alert import TelegramNotifier
from .services.order_processor import process_paid_order

logger = logging.getLogger("autostars")


def setup_logging(verbose: bool = False, log_file: Optional[str] = None) -> None:
    """Настройка логирования."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


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
        else:
            print(f"[WARN] Gameau API вернул ответ: {acc}")
    except Exception as exc:
        print(f"[ERR] Gameau: {exc}")
        all_ok = False

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


def print_deposit_info(cfg: Config) -> None:
    """Инструкция по заводу криптовалюты USDT TRC-20 на сайт gameau.us."""
    print("\n💎 === РУКОВОДСТВО ПО ЗАВОДУ КРИПТЫ USDT TRC-20 НА GAMEAU.US ===")
    print("""
Для автоматической покупки Telegram Stars бот списывает USDT с баланса Gameau.
В кабинете gameau.us перейдите: Профиль -> Пополнить баланс -> USDT TRC-20.
Скопируйте ваш депозитный адрес кошелька в сети TRON (начинается на 'T...').

--------------------------------------------------------------------------------
1️⃣ ВАРИАНТ 1: АНОНИМНЫЙ ОБМЕН / P2P (Курс ~110.00 RUB за 1 USDT)
--------------------------------------------------------------------------------
• Плюсы: Полная анонимность, не требует верификации по паспорту.
• Минусы: Высокий курс (переплата ~22.37 ₽ на каждый USDT), риски P2P блокировок карт.
• Способы покупки:
    1. Telegram Wallet P2P (@wallet) / Telegram Crypto Bot (@send).
    2. Агрегаторы BestChange (направление: Сбербанк/Тинькофф/СБП -> Tether TRC20).
    3. Криптоматы / Cash-in наличными.
• Расчет:
    - 1 000 Stars (9.10 USDT) = 1 001.00 RUB себестоимость.
    - При продаже за 1 370.40 RUB чистая прибыль = +369.40 RUB.

--------------------------------------------------------------------------------
2️⃣ ВАРИАНТ 2: БЕЛАРУСЬ WHITEBIRD БЕЗ P2P (Курс ~87.63 RUB за 1 USDT) [РЕКОМЕНДУЕТСЯ]
--------------------------------------------------------------------------------
• Плюсы: Максимальная прибыль (+173.37 ₽ экономии на каждом заказе!), 
         официальный легальный обменник (ПВТ Беларусь), нет блокировок 115-ФЗ.
• Минусы: Требуется разовая быстрая верификация (KYC).
• Инструкция:
    1. Зайдите на официальный сайт https://whitebird.io
    2. Выберите покупку USDT (TRC-20) с банковской карты (RUB / BYN / USD).
    3. В поле адреса получателя укажите адрес TRC-20 из личного кабинета gameau.us.
    4. Оплатите заказ — USDT моментально поступают на баланс Gameau без посредников!
• Расчет:
    - 1 000 Stars (9.10 USDT) = 797.43 RUB себестоимость.
    - При продаже за 1 370.40 RUB чистая прибыль = +572.97 RUB.

--------------------------------------------------------------------------------
⚡ КОМИССИЯ СЕТИ TRON (ENERGY):
• При прямом выводе с бирж/Whitebird комиссия вывода обычно составляет 1-1.5 USDT.
• При частых переводах с личного кошелька используйте аренду Tron Energy 
  (Feee.io / JustLend) для снижения комиссии за транзакцию с 28 TRX до 6-8 TRX.
================================================================================\n""")


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


async def run_test_order(
    cfg: Config,
    username: str,
    quantity: int = 50,
) -> None:
    """Выполняет тестовый заказ для проверки интеграции (Шаг 6)."""
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
    )

    test_order_data = {
        "id": f"TEST_{int(asyncio.get_event_loop().time())}",
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
        )
        print(f"Результат тестового заказа: {result}")
    finally:
        await db.close()
        await funpay.close()


async def run_bot(cfg: Config, once: bool = False) -> None:
    """Главный асинхронный цикл обработки заказов."""
    cfg.validate()

    db = DBManager(cfg.db_path)
    await db.init_db()

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

    logger.info("Инициализация сессии FunPay...")
    await funpay.login()
    logger.info("Сессия FunPay активна. Запуск Long Polling / очереди заказов.")

    tasks: set[asyncio.Task] = set()

    try:
        while True:
            try:
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
                            )
                        )
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)

                if once:
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    logger.info("Однократный проход завершен.")
                    break

                # Опрашиваем Long Polling runner/
                await funpay.poll_runner()

            except Exception as exc:
                logger.error(f"Ошибка в цикле автовыдачи: {exc}", exc_info=True)

            await asyncio.sleep(cfg.poll_interval)

    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await funpay.close()
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
    setup_logging(verbose=args.verbose, log_file=cfg.log_file)

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
