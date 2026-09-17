"""Главная бизнес-логика обработки заказов (FunPay -> Parsing -> Gameau -> Ответ FunPay -> TG Alert)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .parser import extract_stars_quantity, extract_telegram_username

logger = logging.getLogger("autostars.order_processor")


async def process_paid_order(
    order_data: Dict[str, Any],
    funpay_client: Any,
    gameau_client: Any,
    db: Any,
    tg_notifier: Any,
    default_quantity: int = 1000,
    max_charge_usdt: float = 9.50,
    whitebird_rate: float = 87.63,
) -> Dict[str, Any]:
    """
    Обрабатывает оплаченный заказ с FunPay.

    1. Проверка идемпотентности в БД.
    2. Извлечение Telegram username.
    3. Вызов Gameau API с Idempotency-Key и контролем maxCharge.
    4. Отправка подтверждения покупателю на FunPay.
    5. Telegram alert владельцу с расчётом чистой прибыли.
    """
    order_id = str(order_data["id"])
    chat_node = order_data.get("chat_node") or order_id
    user_message = order_data.get("last_message") or ""
    description = order_data.get("description") or ""

    # 1. Проверяем, не обрабатывался ли заказ ранее (защита локальной БД)
    if await db.is_order_processed(order_id):
        logger.debug(f"[ORDER {order_id}] Заказ уже обработан или находится в обработке. Пропуск.")
        return {"status": "already_processed", "order_id": order_id}

    # Если сообщение пустое, пробуем получить из описания или истории чата
    if not user_message:
        user_message = description
    if not user_message and hasattr(funpay_client, "get_last_buyer_message"):
        chat_msg = await funpay_client.get_last_buyer_message(chat_node)
        if chat_msg:
            user_message = chat_msg

    # 2. Парсим Telegram username
    username = extract_telegram_username(user_message)
    if not username and description and description != user_message:
        username = extract_telegram_username(description)

    if not username:
        # Отправляем авто-ответ с просьбой уточнить никнейм
        logger.info(f"[ORDER {order_id}] Юзернейм не найден. Отправка запроса покупателю.")
        reply_clarify = (
            "Здравствуйте! Не удалось автоматически распознать ваш Telegram @username. "
            "Пожалуйста, напишите его ответным сообщением в формате: @username"
        )
        await funpay_client.send_message(node=chat_node, text=reply_clarify)
        await db.save_order_status(
            order_id=order_id,
            username=None,
            status="WAITING_USERNAME",
            chat_node=str(chat_node),
            quantity=default_quantity,
        )
        return {"status": "waiting_username", "order_id": order_id}

    # Определяем количество звёзд
    quantity = (
        order_data.get("quantity")
        or extract_stars_quantity(description)
        or default_quantity
    )

    # 3. Фиксируем статус 'PROCESSING' в БД
    await db.save_order_status(
        order_id=order_id,
        username=username,
        status="PROCESSING",
        chat_node=str(chat_node),
        quantity=quantity,
    )

    # 4. Вызываем API Gameau (1000 Stars = 9.10 USDT)
    result = await gameau_client.buy_telegram_stars(
        username=username,
        quantity=quantity,
        order_id=order_id,
        max_charge_usdt=max_charge_usdt,
    )

    # Логируем ключ идемпотентности
    if "idempotency_key" in result:
        await db.log_idempotency(
            idempotency_key=result["idempotency_key"],
            order_id=order_id,
            request_payload=f"username={username}&qty={quantity}",
            response_payload=str(result.get("data") or result.get("error")),
            status="SUCCESS" if result.get("success") else "FAILED",
        )

    if result["success"]:
        # 5. Успешно! Вычисляем прибыль, обновляем статус в БД и закрываем сделку на FP
        order_price_rub = float(order_data.get("price") or 0.0)
        usdt_cost = float((result.get("data") or {}).get("charge") or 9.10)

        if order_price_rub > 0:
            profit_rub = tg_notifier.calculate_profit(
                order_price_rub=order_price_rub,
                usdt_cost=usdt_cost,
            )
        else:
            profit_rub = 572.97

        await db.save_order_status(
            order_id=order_id,
            username=username,
            status="COMPLETED",
            chat_node=str(chat_node),
            quantity=quantity,
            price_rub=order_price_rub,
            cost_usdt=usdt_cost,
            profit_rub=profit_rub,
        )

        formatted_quantity = f"{quantity:,}".replace(",", " ")
        reply_text = (
            f"✅ Здравствуйте! {formatted_quantity} Telegram Stars успешно зачислены на аккаунт @{username}.\n\n"
            f"Пожалуйста, проверьте баланс в Telegram и подтвердите успешное выполнение заказа на FunPay! Спасибо за покупку!"
        )
        await funpay_client.send_message(node=chat_node, text=reply_text)

        # Уведомляем владельца в TG-бота
        await tg_notifier.send_alert(
            f"💰 Заказ #{order_id} выполнен! +{profit_rub:.2f} RUB чистой прибыли (Отправлено @{username})"
        )
        return {"status": "completed", "order_id": order_id, "username": username, "profit_rub": profit_rub}

    elif result["error"] == "LOW_BALANCE":
        # Критическая ошибка баланса — высылаем алерт владельцу
        await db.save_order_status(
            order_id=order_id,
            username=username,
            status="FAILED_LOW_BALANCE",
            chat_node=str(chat_node),
            quantity=quantity,
            error="LOW_BALANCE",
        )
        await tg_notifier.send_alert(
            f"🚨 <b>ОШИБКА: НИЗКИЙ БАЛАНС GAMEAU!</b> Заказ #{order_id} остановлен. Срочно пополните USDT через Whitebird!"
        )
        return {"status": "failed", "error": "LOW_BALANCE", "order_id": order_id}

    elif result["error"] == "PRICE_EXCEEDED":
        await db.save_order_status(
            order_id=order_id,
            username=username,
            status="FAILED_PRICE_EXCEEDED",
            chat_node=str(chat_node),
            quantity=quantity,
            error="PRICE_EXCEEDED",
        )
        await tg_notifier.send_alert(
            f"⚠️ <b>ВНИМАНИЕ:</b> Превышен лимит стоимости maxCharge ({max_charge_usdt} USDT) для заказа #{order_id}!"
        )
        return {"status": "failed", "error": "PRICE_EXCEEDED", "order_id": order_id}

    else:
        err = result.get("error", "UNKNOWN_ERROR")
        await db.save_order_status(
            order_id=order_id,
            username=username,
            status="FAILED",
            chat_node=str(chat_node),
            quantity=quantity,
            error=str(err),
        )
        await tg_notifier.send_alert(
            f"❌ <b>ОШИБКА:</b> Сбой выдачи заказа #{order_id} для @{username}: {err}"
        )
        return {"status": "failed", "error": err, "order_id": order_id}
