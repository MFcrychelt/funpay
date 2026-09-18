"""Главная бизнес-логика обработки заказов (FunPay -> Parsing -> Gameau Catalog & Buy -> Ответ FunPay -> TG Alert).

v2.1:
- Полный трекинг выполнения задачи (журнал событий через TaskTracker)
- Повторные попытки при сетевых сбоях (retry с backoff)
- Ожидание финального статуса GAMEAU (wait_for_completion) и себестоимость
  по фактическому списанию (chargedAmount), а не по фиксированному 9.10 USDT
- Сохранение gameau_order_id для авто-согласования (reconciliation)
- Алерт с оценкой убытков при провале заказа
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .parser import extract_stars_quantity, extract_telegram_username
from .task_tracker import (
    EV_COMPLETED,
    EV_FAILED,
    EV_FUNPAY_REPLY,
    EV_GAMEAU_COMPLETED,
    EV_GAMEAU_CREATED,
    EV_GAMEAU_FAILED,
    EV_PACKAGE_SELECTED,
    EV_PARSED_FAIL,
    EV_PARSED_OK,
    EV_RECEIVED,
    EV_RETRY,
    EV_TG_ALERT,
    EV_WAITING_USERNAME,
    TaskTracker,
)

logger = logging.getLogger("autostars.order_processor")

# Коды ошибок GAMEAU, при которых безопасно повторять запрос (сетевые/временные)
TRANSIENT_ERRORS = {
    "NETWORK_TIMEOUT",
    "API_ERROR_429",
    "API_ERROR_502",
    "API_ERROR_503",
    "API_ERROR_504",
}


def _tracker_or_null(tracker: TaskTracker | None) -> TaskTracker:
    """TaskTracker, а при отсутствии — no-op заглушка (созвимость со старыми вызовами)."""
    if tracker is not None:
        return tracker

    class _NullTracker:
        async def log(self, order_id: str, event: str, detail: str = "") -> None:
            logger.debug(f"[TASK {order_id}] {event} {detail}")

    return _NullTracker()  # type: ignore[return-value]


def _extract_gameau_order_id(result: dict[str, Any]) -> str:
    """Достаёт ID заказа GAMEAU из ответа (варианты ключей)."""
    data = result.get("data") or {}
    return str(
        data.get("id")
        or data.get("orderId")
        or data.get("order_id")
        or result.get("order_id")
        or ""
    )


def _extract_charged_usdt(final: dict[str, Any], fallback_usdt: float) -> float:
    """Фактическая себестоимость в USDT по ответу GAMEAU (chargedAmount/amount/charge)."""
    for key in ("chargedAmount", "amount", "charge"):
        val = final.get(key)
        if val is not None:
            try:
                num = float(val)
                if num > 0:
                    return num
            except (TypeError, ValueError):
                continue
    return fallback_usdt


async def _resolve_charge_limit(
    *,
    order_data: dict[str, Any],
    gameau_client: Any,
    quantity: int,
    default_limit: float,
    usdt_per_1000_stars: float,
    margin_pct: float,
) -> tuple[float, float, str]:
    """Возвращает (maxCharge, оценочная себестоимость в USDT, имя пакета).

    maxCharge обязан покрывать реальный пакет: дефолтный лимит рассчитан на
    1000⭐, и для заказа в 5000⭐ он гарантированно дал бы PRICE_EXCEEDED
    (то есть не выданную выдачу и спор на FunPay). Поэтому лимит =
    max(настроенный, цена пакета * (1 + запас)).
    """
    fallback_price = round(quantity / 1000.0 * usdt_per_1000_stars, 4)
    charge_limit = float(order_data.get("max_charge_usdt") or default_limit)
    package_name = ""

    try:
        package = await gameau_client.find_stars_package(quantity) if gameau_client else None
    except Exception as exc:
        logger.debug(f"[ORDER {order_data.get('id')}] Проверка каталога пропущена: {exc}")
        package = None

    if package:
        price = None
        getter = getattr(gameau_client, "item_price_usdt", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                price = getter(package)
        if price is None:
            with contextlib.suppress(TypeError, ValueError):
                price = float(package.get("price") or 0.0) or None
        if price:
            fallback_price = float(price)
            charge_limit = round(max(charge_limit, float(price) * (1.0 + margin_pct / 100.0)), 2)
            package_name = str(package.get("name") or package.get("title") or "package")

    return round(charge_limit, 2), round(fallback_price, 4), package_name


async def process_paid_order(
    order_data: dict[str, Any],
    funpay_client: Any,
    gameau_client: Any,
    db: Any,
    tg_notifier: Any,
    default_quantity: int = 1000,
    max_charge_usdt: float = 9.50,
    whitebird_rate: float = 87.63,
    hide_sender: bool = False,
    task_tracker: TaskTracker | None = None,
    max_order_retries: int = 2,
    wait_completion_timeout: float = 120.0,
    bypass_processed_check: bool = False,
    usdt_per_1000_stars: float = 9.10,
    max_charge_margin_pct: float = 5.0,
) -> dict[str, Any]:
    """
    Обрабатывает оплаченный заказ с FunPay.

    1. Проверка идемпотентности в БД.
    2. Извлечение Telegram username (трекинг PARSED_*).
    3. Синхронизация с каталогом Gameau (при наличии).
    4. Вызов Gameau API с Idempotency-Key и контролем maxCharge
       (повторные попытки при сетевых сбоях).
    5. Ожидание финального статуса GAMEAU, себестоимость по фактическому списанию.
    6. Отправка подтверждения покупателю на FunPay.
    7. Telegram alert владельцу с расчётом прибыли/убытков.
    """
    tracker = _tracker_or_null(task_tracker)
    order_id = str(order_data["id"])
    chat_node = order_data.get("chat_node") or order_id
    user_message = order_data.get("last_message") or ""
    description = order_data.get("description") or ""

    # 0. Заказ получен
    await tracker.log(order_id, EV_RECEIVED, f"price={order_data.get('price') or 0}₽")

    # 1. Проверяем, не обрабатывался ли заказ ранее (защита локальной БД).
    # bypass_processed_check — для чат-мониторинга (WAITING_USERNAME) и ретраев:
    # повторный вход в пайплайн по инициативе оператора/системы.
    if not bypass_processed_check and await db.is_order_processed(order_id):
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
        await tracker.log(order_id, EV_PARSED_FAIL, "username не распознан")
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
            price_rub=float(order_data.get("price") or 0.0),
        )
        await tracker.log(order_id, EV_WAITING_USERNAME, "покупателю отправлен запрос ника")
        return {"status": "waiting_username", "order_id": order_id}

    # Определяем количество звёзд
    quantity = (
        order_data.get("quantity")
        or extract_stars_quantity(description)
        or extract_stars_quantity(user_message)
        or default_quantity
    )
    await tracker.log(
        order_id, EV_PARSED_OK, f"username=@{username}, quantity={quantity}"
    )

    # Цена пакета из каталога: по ней считаем и лимит списания, и запасную
    # себестоимость (когда GAMEAU не вернул chargedAmount).
    charge_limit, fallback_price, package_name = await _resolve_charge_limit(
        order_data=order_data,
        gameau_client=gameau_client,
        quantity=quantity,
        default_limit=float(max_charge_usdt),
        usdt_per_1000_stars=float(usdt_per_1000_stars),
        margin_pct=float(max_charge_margin_pct),
    )
    if package_name:
        await tracker.log(order_id, EV_PACKAGE_SELECTED, f"{package_name} = {fallback_price} USDT")

    # 3. Фиксируем статус 'PROCESSING' в БД
    await db.save_order_status(
        order_id=order_id,
        username=username,
        status="PROCESSING",
        chat_node=str(chat_node),
        quantity=quantity,
        price_rub=float(order_data.get("price") or 0.0),
    )

    # 4. Вызываем API Gameau с повторными попытками при сетевых сбоях
    result: dict[str, Any] = {}
    attempts = max(1, int(max_order_retries) + 1)
    for attempt in range(1, attempts + 1):
        result = await gameau_client.buy_telegram_stars(
            username=username,
            quantity=quantity,
            order_id=order_id,
            max_charge_usdt=charge_limit,
            hide_sender=hide_sender,
        )

        if result.get("success"):
            break

        err = str(result.get("error") or "")
        if err in TRANSIENT_ERRORS and attempt < attempts:
            backoff = 2 ** attempt
            logger.warning(
                f"[ORDER {order_id}] Временная ошибка ({err}), "
                f"повтор {attempt}/{max_order_retries} через {backoff}s..."
            )
            await tracker.log(order_id, EV_RETRY, f"{err}, попытка {attempt + 1}")
            await asyncio.sleep(backoff)
            continue
        break

    gameau_order_id = _extract_gameau_order_id(result)

    # Логируем ключ идемпотентности
    if "idempotency_key" in result:
        await db.log_idempotency(
            idempotency_key=result["idempotency_key"],
            order_id=order_id,
            request_payload=f"username={username}&qty={quantity}&maxCharge={charge_limit}",
            response_payload=str(result.get("data") or result.get("error"))[:2000],
            status="SUCCESS" if result.get("success") else "FAILED",
        )

    if result["success"]:
        await tracker.log(
            order_id, EV_GAMEAU_CREATED,
            f"gameau_id={gameau_order_id or '?'} maxCharge={charge_limit} USDT",
        )

        # 5. Ожидаем финальный статус (звёзды реально выданы) — неблокируемо
        final_data: dict[str, Any] = result.get("data") or {}
        waited = False
        if gameau_order_id and hasattr(gameau_client, "wait_for_completion"):
            try:
                final_data = await gameau_client.wait_for_completion(
                    gameau_order_id, poll_interval=3.0, timeout=wait_completion_timeout
                ) or final_data
                waited = True
            except RuntimeError as exc:
                # Финальный статус - ошибка со стороны GAMEAU
                err_status = str(exc)
                final_data = final_data or {}
                usdt_cost = _extract_charged_usdt(final_data, fallback_price)
                await db.save_order_status(
                    order_id=order_id,
                    username=username,
                    status="FAILED_DELIVERY",
                    quantity=quantity,
                    cost_usdt=round(usdt_cost, 4),
                    error=err_status[:500],
                    gameau_order_id=gameau_order_id or None,
                )
                await tracker.log(order_id, EV_GAMEAU_FAILED, err_status[:300])
                await tracker.log(order_id, EV_FAILED, "GAMEAU delivery failed")
                await _alert_delivery_failed(order_id, username, quantity, err_status,
                                             usdt_cost, order_data.get("price"), tg_notifier, tracker)
                return {"status": "failed", "error": "GAMEAU_DELIVERY_FAILED", "order_id": order_id}
            except Exception as exc:
                logger.warning(f"[ORDER {order_id}] wait_for_completion завершился с ошибкой: {exc}")

        final_status = str(final_data.get("status") or "").lower()

        if final_status in ("failed", "refunded", "cancelled", "canceled", "rejected"):
            err_status = f"GAMEAU_STATUS_{final_status}"
            usdt_cost = _extract_charged_usdt(final_data, fallback_price)
            await db.save_order_status(
                order_id=order_id,
                username=username,
                status="FAILED_DELIVERY",
                quantity=quantity,
                cost_usdt=round(usdt_cost, 4),
                error=err_status,
                gameau_order_id=gameau_order_id or None,
            )
            await tracker.log(order_id, EV_GAMEAU_FAILED, err_status)
            await tracker.log(order_id, EV_FAILED, err_status)
            await _alert_delivery_failed(order_id, username, quantity, err_status,
                                         usdt_cost, order_data.get("price"), tg_notifier, tracker)
            return {"status": "failed", "error": "GAMEAU_DELIVERY_FAILED", "order_id": order_id}

        if waited and final_status not in (
            "success", "completed", "complete", "delivered", "paid"
        ):
            # Выдачу не удалось подтвердить (timeout/unknown/processing) —
            # НЕ помечаем COMPLETED, оставляем задачу в работе:
            # reconciliation досинхронизирует по gameau_order_id.
            await db.save_order_status(
                order_id=order_id,
                username=username,
                status="PROCESSING",
                quantity=quantity,
                gameau_order_id=gameau_order_id or None,
                error=f"UNCONFIRMED_STATUS_{final_status or 'UNKNOWN'}",
            )
            await tracker.log(
                order_id, "GAMEAU_UNCONFIRMED",
                f"статус '{final_status or 'пустой'}' после ожидания {int(wait_completion_timeout)}с",
            )
            logger.warning(
                f"[ORDER {order_id}] Не удалось подтвердить выдачу GAMEAU "
                f"(статус '{final_status}'), оставляем задачу в работе (reconciliation)"
            )
            try:
                await tg_notifier.send_alert(
                    f"⏳ <b>ЗАДАЧА В РАБОТЕ:</b> заказ #{order_id} @{username} "
                    f"(GAMEAU {gameau_order_id}) — статус не подтвержден "
                    f"({final_status or 'нет ответа'}). Проверяем автоматически, "
                    f"при необходимости разбирается вручную."
                )
            except Exception as exc:
                logger.warning(f"[ORDER {order_id}] Не удалось отправить алерт: {exc}")
            return {"status": "unconfirmed", "order_id": order_id,
                    "gameau_order_id": gameau_order_id}

        # 6. Успешно! Вычисляем прибыль, обновляем статус в БД и закрываем сделку на FP
        order_price_rub = float(order_data.get("price") or 0.0)
        usdt_cost = _extract_charged_usdt(final_data, fallback_price)

        if hasattr(tg_notifier, "calculate_profit") and order_price_rub > 0:
            profit_rub = tg_notifier.calculate_profit(order_price_rub=order_price_rub, usdt_cost=usdt_cost)
        else:
            profit_rub = 0.0

        # Себестоимость в рублях по активному курсу
        rate = getattr(tg_notifier, "current_rate", whitebird_rate) or whitebird_rate
        cost_rub = round(usdt_cost * float(rate), 2)

        await db.save_order_status(
            order_id=order_id,
            username=username,
            status="COMPLETED",
            chat_node=str(chat_node),
            quantity=quantity,
            price_rub=order_price_rub,
            cost_usdt=round(usdt_cost, 4),
            cost_rub=cost_rub,
            profit_rub=profit_rub,
            gameau_order_id=gameau_order_id or None,
        )
        await tracker.log(
            order_id, EV_GAMEAU_COMPLETED,
            f"charge={usdt_cost} USDT cost={cost_rub}₽ profit={profit_rub}₽",
        )
        await tracker.log(order_id, EV_COMPLETED, f"profit={profit_rub}₽")

        formatted_quantity = f"{quantity:,}".replace(",", " ")
        reply_text = (
            f"✅ Здравствуйте! {formatted_quantity} Telegram Stars успешно зачислены на аккаунт @{username}.\n\n"
            f"Пожалуйста, проверьте баланс в Telegram и подтвердите успешное выполнение заказа на FunPay! Спасибо за покупку!"
        )
        if await funpay_client.send_message(node=chat_node, text=reply_text):
            await tracker.log(order_id, EV_FUNPAY_REPLY, "покупателю отправлено подтверждение")
        else:
            logger.warning(f"[ORDER {order_id}] Не удалось отправить сообщение покупателю")

        # Уведомляем владельца в TG-бота с анализом прибыльности обоих вариантов
        if hasattr(tg_notifier, "alert_order_completed"):
            await tg_notifier.alert_order_completed(
                order_id=order_id,
                username=username,
                profit_rub=profit_rub,
                order_price_rub=order_price_rub,
                usdt_cost=usdt_cost,
            )
        else:
            await tg_notifier.send_alert(
                f"💰 Заказ #{order_id} выполнен! +{profit_rub:.2f} RUB чистой прибыли (Отправлено @{username})"
            )
        await tracker.log(order_id, EV_TG_ALERT, "уведомлен владелец")
        return {
            "status": "completed",
            "order_id": order_id,
            "username": username,
            "profit_rub": profit_rub,
            "gameau_order_id": gameau_order_id,
        }

    elif result["error"] == "LOW_BALANCE":
        # Критическая ошибка баланса — высылаем алерт владельцу с инструкциями по пополнению USDT TRC-20
        await db.save_order_status(
            order_id=order_id,
            username=username,
            status="FAILED_LOW_BALANCE",
            chat_node=str(chat_node),
            quantity=quantity,
            error="LOW_BALANCE",
            gameau_order_id=gameau_order_id or None,
        )
        await tracker.log(order_id, EV_FAILED, "LOW_BALANCE")
        if hasattr(tg_notifier, "alert_low_balance"):
            await tg_notifier.alert_low_balance(order_id)
        else:
            await tg_notifier.send_alert(
                f"🚨 <b>ОШИБКА: НИЗКИЙ БАЛАНС GAMEAU!</b> Заказ #{order_id} остановлен. Срочно пополните USDT через Whitebird!"
            )
        await tracker.log(order_id, EV_TG_ALERT, "критический алерт LOW_BALANCE")
        return {"status": "failed", "error": "LOW_BALANCE", "order_id": order_id}

    elif result["error"] in ("PRICE_EXCEEDED",):
        await db.save_order_status(
            order_id=order_id,
            username=username,
            status="FAILED_PRICE_EXCEEDED",
            chat_node=str(chat_node),
            quantity=quantity,
            error="PRICE_EXCEEDED",
            gameau_order_id=gameau_order_id or None,
        )
        await tracker.log(order_id, EV_FAILED, "PRICE_EXCEEDED")
        if hasattr(tg_notifier, "alert_price_exceeded"):
            await tg_notifier.alert_price_exceeded(order_id, charge_limit)
        else:
            await tg_notifier.send_alert(
                f"⚠️ <b>ВНИМАНИЕ:</b> Превышен лимит стоимости maxCharge ({charge_limit} USDT) для заказа #{order_id}!"
            )
        await tracker.log(order_id, EV_TG_ALERT, "алерт PRICE_EXCEEDED")
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
            gameau_order_id=gameau_order_id or None,
        )
        await tracker.log(order_id, EV_FAILED, str(err))
        # Оценка убытка: потерянная потенциальная выручка заказа
        await _alert_delivery_failed(order_id, username, quantity, str(err),
                                     0.0, order_data.get("price"), tg_notifier, tracker)
        return {"status": "failed", "error": err, "order_id": order_id}


async def _alert_delivery_failed(
    order_id: str,
    username: str | None,
    quantity: int,
    err: str,
    wasted_usdt: float,
    order_price_rub: Any,
    tg_notifier: Any,
    tracker: TaskTracker,
) -> None:
    """Алерт владельцу о провале задачи с оценкой убытков."""
    try:
        price = float(order_price_rub or 0.0)
        parts = [
            f"❌ <b>ОШИБКА ВЫДАЧИ — ЗАДАЧА НЕ ВЫПОЛНЕНА</b>\n"
            f"🆔 Заказ: #{order_id}\n"
            f"👤 Получатель: @{username or 'не указан'}\n"
            f"⭐ Количество: {quantity} шт.\n"
            f"❗ Ошибка: {err}\n"
        ]
        if price > 0:
            parts.append(f"💸 Потерянная выручка: {price:,.2f} ₽".replace(",", " "))
        if wasted_usdt > 0:
            parts.append(f"🔥 Списано у GAMEAU без выдачи: {wasted_usdt:.4f} USDT")
        parts.append(
            "\nРекомендации:\n"
            "1. Проверьте баланс GAMEAU\n"
            "2. Убедитесь в правильности логина\n"
            "3. Разберите заказ вручную и отмените/верните деньги на FunPay"
        )
        if hasattr(tg_notifier, "send_alert"):
            await tg_notifier.send_alert("\n".join(parts))
            await tracker.log(order_id, EV_TG_ALERT, f"алерт об ошибке: {str(err)[:80]}")
    except Exception as exc:
        logger.warning(f"[ORDER {order_id}] Не удалось отправить алерт об ошибке: {exc}")
