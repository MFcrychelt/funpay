#!/usr/bin/env python3
"""Точка входа бота автовыдачи:

    python main.py            # бесконечный цикл автовыдачи
    python main.py --once     # один прогон по текущей очереди заказов
    python main.py --check    # проверить конфиг и доступ к FunPay / GAMEAU

Секреты — в файле `.env` (см. `.env.example`). Настройки — в `config.json`.
"""

from __future__ import annotations

import argparse
import os
import sys

from bot.config import Config
from bot.delivery import DeliveryEngine
from bot.funpay_client import FunPayClient, FunPayError
from bot.gameau import GameauClient, GameauError
from bot.logger_setup import get_logger, setup_logging
from bot.state import State

logger = get_logger("main")


def build_engine(cfg: Config) -> tuple[FunPayClient, GameauClient, DeliveryEngine]:
    """Создаёт и логинит все компоненты."""
    cfg.validate()
    funpay = FunPayClient(cfg)
    funpay.login()
    gameau = GameauClient(
        api_key=cfg.gameau_api_key,
        base_url=cfg.gameau_base_url,
        timeout=cfg.gameau_request_timeout,
    )
    state_file = os.environ.get("FUNPAY_STARS_STATE", "state.json")
    state = State(state_file)
    engine = DeliveryEngine(cfg, funpay, gameau, state)
    return funpay, gameau, engine


def cmd_check(cfg: Config) -> int:
    """Диагностика: вход в FunPay, доступ к GAMEAU, баланс, каталог звёзд."""
    ok = True

    # FunPay
    funpay = FunPayClient(cfg)
    try:
        funpay.login()
        acc = funpay.account
        print(f"[OK]  FunPay: вошли как {acc.username} (ID {acc.id}), "
              f"активные продажи: {acc.active_sales}")
    except FunPayError as exc:
        print(f"[ERR] FunPay: {exc}")
        ok = False

    # GAMEAU
    gameau = GameauClient(api_key=cfg.gameau_api_key, base_url=cfg.gameau_base_url,
                          timeout=cfg.gameau_request_timeout)
    try:
        account = gameau.get_account()
        print(f"[OK]  GAMEAU: аккаунт {account.get('username')}, "
              f"баланс {account.get('balance')} {account.get('currency')}, "
              f"тариф {account.get('plan')}, статус {account.get('status')}")
    except GameauError as exc:
        print(f"[ERR] GAMEAU: {exc}")
        ok = False

    try:
        items = gameau.get_stars_catalog()
        packages = sorted(
            (s, p) for s, p in
            ((gameau.package_stars(i), i.get("price")) for i in items)
            if s
        )
        if packages:
            preview = ", ".join(f"{s}★=${p}" for s, p in packages[:12])
            more = f" (+{len(packages) - 12})" if len(packages) > 12 else ""
            print(f"[OK]  GAMEAU каталог звёзд: {preview}{more}")
        else:
            print("[WARN] GAMEAU каталог звёзд пуст — проверьте тип 'telegramStars'.")
    except GameauError as exc:
        print(f"[ERR] GAMEAU каталог: {exc}")
        ok = False

    # Конфиг правил
    if cfg.lot_rules:
        print(f"[OK]  Настроено правил лотов: {len(cfg.lot_rules)}")
    elif cfg.default_stars:
        print(f"[OK]  default_stars={cfg.default_stars}")
    else:
        print("[WARN] Правила лотов и default_stars не заданы — количество звёзд будет "
              "определяться только из названия лота («100 звёзд», «250 stars»).")

    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Автовыдача Telegram Stars на FunPay через GAMEAU.",
    )
    parser.add_argument("--once", action="store_true",
                        help="один прогон по очереди заказов и выход")
    parser.add_argument("--check", action="store_true",
                        help="проверить конфиг и доступы, затем выйти")
    parser.add_argument("--config", default="config.json",
                        help="путь к файлу конфигурации (по умолчанию config.json)")
    parser.add_argument("--verbose", "-v", action="store_true", help="отладочный вывод")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    setup_logging(log_file=cfg.log_file, verbose=args.verbose)

    if args.check:
        try:
            cfg.validate()
        except SystemExit as exc:
            print(exc)
            return 1
        return cmd_check(cfg)

    try:
        _, _, engine = build_engine(cfg)
    except FunPayError as exc:
        logger.error("FunPay: %s", exc)
        return 1
    except SystemExit as exc:
        logger.error("%s", exc)
        return 1

    if args.once:
        count = engine.process_pending_once()
        logger.info("Однократный прогон завершён. Заказов в очереди: %d", count)
        return 0

    try:
        engine.run()
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
