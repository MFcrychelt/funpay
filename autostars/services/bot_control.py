"""Управление ботом в runtime (v2.2):

- Пауза/резюм обработки новых заказов (флаг в settings БД)
- Чат-мониторинг: заказы WAITING_USERNAME — подхватываем @username,
  который покупатель написал в чате FunPay (отзывчивость без ручного участия)
- Ретрай проваленных заказов (повтор того же заказа, идемпотентно)
- Контроль баланса GAMEAU с алертом по порогу
"""

from __future__ import annotations

import logging
from typing import Any

from ..database.db_manager import RETRYABLE_STATUSES
from .order_processor import process_paid_order
from .parser import extract_stars_quantity, extract_telegram_username
from .task_tracker import TaskTracker

logger = logging.getLogger("autostars.control")

PAUSED_SETTING_KEY = "bot_paused"


# --------------------------------------------------------------------------- #
# Пауза / резюм
# --------------------------------------------------------------------------- #

async def is_bot_paused(db: Any) -> bool:
    """Флаг паузы в settings БД (переживает перезапуск процесса)."""
    return (await db.get_setting(PAUSED_SETTING_KEY, "0")) == "1"


async def set_bot_paused(db: Any, paused: bool) -> bool:
    await db.set_setting(PAUSED_SETTING_KEY, "1" if paused else "0")
    logger.info(f"Бот {'ПАУЗИРОВАН' if paused else 'ВОЗОБНОВЛЕН'} (settings)")
    return paused


# --------------------------------------------------------------------------- #
# Чат-мониторинг заказов, ожидающих @username
# --------------------------------------------------------------------------- #

async def process_waiting_username_orders(
    db: Any,
    funpay_client: Any,
    gameau_client: Any,
    tg_notifier: Any,
    tracker: TaskTracker,
    default_quantity: int = 1000,
    max_charge_usdt: float = 9.50,
    whitebird_rate: float = 87.63,
    hide_sender: bool = False,
    max_order_retries: int = 2,
    wait_completion_timeout: float = 120.0,
    limit: int = 20,
    usdt_per_1000_stars: float = 9.10,
    max_charge_margin_pct: float = 5.0,
    policy: Any = None,
) -> int:
    """
    Проходит по заказам в статусе WAITING_USERNAME и смотрит последнее
    сообщение покупателя в чате FunPay. Если там появился @username —
    автоматически продолжает выдачу (без повторного спам-запроса).

    Возвращает количество запущенных на выдачу заказов.
    """
    waiting = await db.get_orders_by_status(["WAITING_USERNAME"], limit=limit)
    started = 0
    for order in waiting:
        order_id = str(order.get("order_id"))
        chat_node = order.get("chat_node") or order_id
        try:
            last_msg = await funpay_client.get_last_buyer_message(chat_node)
        except Exception as exc:
            logger.warning(f"[CHAT-MONITOR] #{order_id}: не удалось прочитать чат: {exc}")
            continue
        if not last_msg:
            continue

        username = extract_telegram_username(last_msg)
        if not username:
            continue

        quantity = extract_stars_quantity(last_msg) or order.get("quantity") or default_quantity
        logger.info(
            f"[CHAT-MONITOR] #{order_id}: покупатель ответил «{last_msg[:80]}» → "
            f"запускаем выдачу @{username} × {quantity}"
        )
        await tracker.log(order_id, "CHAT_USERNAME_RECEIVED", f"msg={last_msg[:200]}")
        try:
            await process_paid_order(
                order_data={
                    "id": order_id,
                    "chat_node": chat_node,
                    "last_message": last_msg,
                    "description": last_msg,
                    "quantity": quantity,
                    "price": order.get("price_rub"),
                },
                funpay_client=funpay_client,
                gameau_client=gameau_client,
                db=db,
                tg_notifier=tg_notifier,
                default_quantity=default_quantity,
                max_charge_usdt=max_charge_usdt,
                whitebird_rate=whitebird_rate,
                hide_sender=hide_sender,
                task_tracker=tracker,
                max_order_retries=max_order_retries,
                wait_completion_timeout=wait_completion_timeout,
                usdt_per_1000_stars=usdt_per_1000_stars,
                max_charge_margin_pct=max_charge_margin_pct,
                bypass_processed_check=True,
                policy=policy,
            )
            started += 1
        except Exception as exc:
            logger.error(f"[CHAT-MONITOR] #{order_id}: ошибка обработки: {exc}", exc_info=True)
    return started


# --------------------------------------------------------------------------- #
# Ретрай проваленных заказов
# --------------------------------------------------------------------------- #

async def retry_failed_order(
    order_id: str,
    db: Any,
    funpay_client: Any,
    gameau_client: Any,
    tg_notifier: Any,
    tracker: TaskTracker,
    default_quantity: int = 1000,
    max_charge_usdt: float = 9.50,
    whitebird_rate: float = 87.63,
    hide_sender: bool = False,
    max_order_retries: int = 2,
    wait_completion_timeout: float = 120.0,
    usdt_per_1000_stars: float = 9.10,
    max_charge_margin_pct: float = 5.0,
    policy: Any = None,
    ignore_policy: bool = False,
) -> dict[str, Any]:
    """
    Повторяет выдачу проваленного или задержанного (HOLD_MANUAL) заказа.

    Идемпотентно:
    Idempotency-Key детерминирован по order_id, поэтому дубль покупки в GAMEAU
    невозможен — API вернёт тот же заказ, если он был создан ранее.
    """
    row = await db.get_order(order_id)
    if row is None:
        return {"status": "not_found", "order_id": order_id}

    current = row.get("status")
    if current not in RETRYABLE_STATUSES:
        return {
            "status": "not_retryable",
            "order_id": order_id,
            "current_status": current,
            "hint": "ретрай доступен для HOLD_MANUAL / FAILED / FAILED_LOW_BALANCE / "
                    "FAILED_PRICE_EXCEEDED / FAILED_DELIVERY / CANCELLED",
        }

    username = row.get("username")
    if not username:
        return {
            "status": "no_username",
            "order_id": order_id,
            "hint": "у заказа нет @username — ждём ответ покупателя "
                    "(чат-мониторинг подхватит автоматически)",
        }

    await tracker.log(order_id, "RETRY_MANUAL", f"повтор из статуса {current}")
    return await process_paid_order(
        order_data={
            "id": order_id,
            "chat_node": row.get("chat_node") or order_id,
            "last_message": f"@{username}",
            "description": f"@{username}",
            "quantity": row.get("quantity") or default_quantity,
            "price": row.get("price_rub"),
        },
        funpay_client=funpay_client,
        gameau_client=gameau_client,
        db=db,
        tg_notifier=tg_notifier,
        default_quantity=default_quantity,
        max_charge_usdt=max_charge_usdt,
        whitebird_rate=whitebird_rate,
        hide_sender=hide_sender,
        task_tracker=tracker,
        max_order_retries=max_order_retries,
        wait_completion_timeout=wait_completion_timeout,
        usdt_per_1000_stars=usdt_per_1000_stars,
        max_charge_margin_pct=max_charge_margin_pct,
        bypass_processed_check=True,
        policy=policy,
        ignore_policy=ignore_policy,
    )


# --------------------------------------------------------------------------- #
# Контроль баланса GAMEAU
# --------------------------------------------------------------------------- #

async def check_gameau_balance(
    gameau_client: Any,
    tg_notifier: Any,
    threshold_usdt: float = 50.0,
    alert: bool = True,
) -> dict[str, Any] | None:
    """
    Проверяет баланс GAMEAU. Если ниже порога — алерт владельцу.
    Возвращает {"balance": float, "currency": str, "plan": str} или None.
    """
    try:
        acc = await gameau_client.get_account()
    except Exception as exc:
        logger.warning(f"[BALANCE] Ошибка запроса баланса GAMEAU: {exc}")
        return None

    if not acc or "error" in acc:
        logger.warning(f"[BALANCE] GAMEAU не вернул баланс: {acc}")
        return None

    try:
        balance = float(acc.get("balance"))
    except (TypeError, ValueError):
        balance = 0.0
    currency = acc.get("currency", "USD")
    info = {
        "balance": balance,
        "currency": currency,
        "plan": acc.get("plan", "Standard"),
        "status": acc.get("status", "active"),
    }

    if alert and balance < threshold_usdt:
        await tg_notifier.send_alert(
            f"🚨 <b>БАЛАНС GAMEAU НИЗКИЙ!</b>\n"
            f"Остаток: <b>{balance:.2f} {currency}</b> (порог: {threshold_usdt:.2f})\n\n"
            f"Пополните USDT TRC-20, чтобы не останавливать выдачу:\n"
            f"• Вариант 2 (Whitebird, ~87.63 ₽/USDT) — рекомендован\n"
            f"• Вариант 1 (анонимный обмен, ~110 ₽/USDT)\n"
            f"Инструкция: python -m autostars.main --deposit-info"
        )
    return info
