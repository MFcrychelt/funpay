"""Точка входа AutoStars (Async Event Loop).

Запуск:
    python -m autostars.main           # Асинхронный цикл автовыдачи
    python -m autostars.main --once    # Однократный опрос очереди
    python -m autostars.main --check   # Проверка подключений и конфигурации
    python -m autostars.main --test-order username=durov [qty=50]  # Тестовый заказ
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
            currency = acc.get("currency", "USDT")
            print(f"[OK] Gameau API доступен. Баланс: {balance} {currency}")
        else:
            print(f"[WARN] Gameau API вернул ответ: {acc}")
    except Exception as exc:
        print(f"[ERR] Gameau: {exc}")
        all_ok = False

    # 4. Telegram Notifier
    notifier = TelegramNotifier(
        bot_token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        whitebird_rate=cfg.whitebird_usdt_rate,
    )
    if notifier.is_configured:
        print(f"[OK] Telegram Alert настроен (chat_id={cfg.telegram_chat_id})")
    else:
        print("[INFO] Telegram Alert не настроен (опционально)")

    print(f"💰 Курс Whitebird USDT: {cfg.whitebird_usdt_rate} RUB/USDT")
    print(f"🛡️ Лимит maxCharge: {cfg.default_max_charge_usdt} USDT")
    print("=========================================\n")
    return all_ok


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
        whitebird_rate=cfg.whitebird_usdt_rate,
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
            whitebird_rate=cfg.whitebird_usdt_rate,
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
        whitebird_rate=cfg.whitebird_usdt_rate,
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
                                whitebird_rate=cfg.whitebird_usdt_rate,
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
