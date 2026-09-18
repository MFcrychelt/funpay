"""Трекинг выполнения задач: жизненный цикл заказов, поиск зависших задач,
авто-согласование (reconciliation) незавершённых заказов с GAMEAU API."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("autostars.tasks")

# События жизненного цикла заказа
EV_RECEIVED = "RECEIVED"                 # заказ получен из очереди FunPay
EV_PARSED_OK = "PARSED_OK"               # успешно распознан username/quantity
EV_PARSED_FAIL = "PARSED_FAIL"           # не распознано
EV_WAITING_USERNAME = "WAITING_USERNAME" # отправлен запрос ника покупателю
EV_PACKAGE_SELECTED = "PACKAGE_SELECTED" # выбран пакет из каталога Gameau
EV_GAMEAU_CREATED = "GAMEAU_CREATED"     # заказ создан в Gameau
EV_GAMEAU_COMPLETED = "GAMEAU_COMPLETED" # Gameau подтвердил выдачу
EV_GAMEAU_FAILED = "GAMEAU_FAILED"       # Gameau отклонил/списал в минус
EV_RETRY = "RETRY"                       # повторная попытка после сетевой ошибки
EV_FUNPAY_REPLY = "FUNPAY_REPLY"         # ответ отправлен покупателю
EV_TG_ALERT = "TG_ALERT"                 # алерт отправлен владельцу
EV_STUCK_ALERTED = "STUCK_ALERTED"       # разовый алерт о зависшей задаче
EV_COMPLETED = "COMPLETED"               # задача закрыта успешно
EV_FAILED = "FAILED"                     # задача закрыта с ошибкой
EV_CANCELLED = "CANCELLED"               # заказ отменён


class TaskTracker:
    """Обёртка над журналом task_events: события, таймлайны, зависшие задачи."""

    def __init__(self, db: Any):
        self.db = db

    async def log(self, order_id: str, event: str, detail: str = "") -> None:
        """Записывает событие и пишет его в общий лог."""
        try:
            await self.db.log_task_event(str(order_id), event, detail)
            logger.info(f"[TASK {order_id}] {event}" + (f" | {detail}" if detail else ""))
        except Exception as exc:  # трекер не должен ронять пайплайн
            logger.warning(f"Не удалось записать событие {event} по {order_id}: {exc}")

    async def timeline(self, order_id: str) -> list[dict[str, Any]]:
        """Полная история выполнения задачи по заказу."""
        return await self.db.get_task_timeline(order_id)

    async def summary(self) -> dict[str, int]:
        """Счётчики по статусам (сводный трекинг)."""
        return await self.db.get_status_counts()

    async def open_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        """Открытые задачи (в работе)."""
        return await self.db.get_in_progress_orders(limit=limit)

    async def stuck_tasks(self, stuck_minutes: float = 20.0) -> list[dict[str, Any]]:
        """Зависшие задачи: в работе, но без обновлений дольше stuck_minutes."""
        return await self.db.get_stuck_orders(stuck_seconds=int(stuck_minutes * 60))

    async def render_text(self, stuck_minutes: float = 20.0) -> str:
        """Текстовая сводка трекинга для консоли."""
        lines: list[str] = []
        now = time.time()
        lines.append("=" * 80)
        lines.append("🗂 ТРЕКИНГ ВЫПОЛНЕНИЯ ЗАДАЧ")
        lines.append("-" * 80)

        counts = await self.summary()
        if counts:
            lines.append("Статусы: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
        else:
            lines.append("Статусы: в базе нет заказов")

        open_tasks = await self.open_tasks(limit=20)
        lines.append(f"\nОткрытые задачи: {len(open_tasks)}")
        for t in open_tasks:
            age_min = int((now - (t.get("updated_ts") or 0)) / 60) if t.get("updated_ts") else "?"
            lines.append(
                f"  ⏳ #{t['order_id']} | {t.get('status')} | @{t.get('username') or 'ник не указан'} | "
                f"{int(t.get('quantity') or 0):,}".replace(",", " ") + f" ⭐ | обновлено {age_min} мин. назад"
            )

        stuck = await self.stuck_tasks(stuck_minutes)
        lines.append(f"\nЗависшие задачи (> {int(stuck_minutes)} мин без движения): {len(stuck)}")
        for t in stuck:
            lines.append(f"  🔴 #{t['order_id']} | {t.get('status')} | gameau={t.get('gameau_order_id') or '—'}")

        lines.append("\nПоследние события журнала:")
        recent = await self.db.get_recent_task_events(limit=15)
        for e in recent:
            ts = time.strftime("%d.%m %H:%M:%S", time.localtime(e.get("ts_epoch") or 0))
            lines.append(f"  {ts} | #{e['order_id']} | {e['event']}" + (f" | {e['detail']}" if e.get("detail") else ""))
        lines.append("=" * 80)
        return "\n".join(lines)


async def reconcile_inflight_orders(
    tracker: TaskTracker,
    db: Any,
    gameau_client: Any,
    funpay_client: Any,
    tg_notifier: Any,
    stuck_minutes: float = 20.0,
) -> int:
    """
    Авто-согласование незавершённых задач с GAMEAU API.

    Для каждого заказа в статусе PROCESSING, у которого есть gameau_order_id:
    - спрашиваем GAMEAU актуальный статус;
    - если заказ выполнен — досчитываем прибыль, ставим COMPLETED,
      отправляем ответ покупателю и алерт владельцу;
    - если отклонён — ставим FAILED и алертим;
    - если "завис" дольше stuck_minutes — разовый предупреждающий алерт.

    Возвращает количество завершённых в этом проходе задач.
    """
    finished = 0
    in_progress = await tracker.open_tasks(limit=100)
    # Список зависших задач считаем один раз
    stuck_ids = {str(o["order_id"]) for o in await tracker.stuck_tasks(stuck_minutes)}

    for order in in_progress:
        order_id = str(order.get("order_id"))
        gameau_id = order.get("gameau_order_id")
        username = order.get("username")
        quantity = int(order.get("quantity") or 0)
        price_rub = float(order.get("price_rub") or 0.0)

        # вложенность осознанна: второй запрос к базе только для зависших заказов
        if order_id in stuck_ids:  # noqa: SIM102
            if not await db.has_recent_task_event(order_id, EV_STUCK_ALERTED, minutes_back=60):
                await tracker.log(order_id, EV_STUCK_ALERTED, f"нет движения {int(stuck_minutes)}+ мин")
                try:
                    await tg_notifier.send_alert(
                        f"⏰ <b>ЗАВИСШАЯ ЗАДАЧА:</b> заказ #{order_id} @{username or '?'} "
                        f"в статусе {order.get('status')} без движения дольше {int(stuck_minutes)} мин. "
                        f"Gameau ID: {gameau_id or 'не получен'}. Проверяем статус у GAMEAU..."
                    )
                except Exception as exc:
                    logger.warning(f"[RECONCILE] Не удалось отправить алерт о зависании: {exc}")

        if not gameau_id:
            # заказ не был создан в GAMEAU (например, упал процесс между этапами)
            continue

        try:
            status_data = await gameau_client.get_order_status(gameau_id)
        except Exception as exc:
            logger.warning(f"[RECONCILE] Ошибка запроса статуса Gameau {gameau_id}: {exc}")
            continue

        status = str(status_data.get("status") or "").lower()
        if status in ("", "processing", "unknown"):
            continue

        from ..clients.gameau import FAILED_STATUSES, SUCCESS_STATUSES

        if status in SUCCESS_STATUSES:
            # Доп. расчёт: себестоимость по фактическому списанию
            charge = float(
                status_data.get("chargedAmount")
                or status_data.get("amount")
                or 0.0
            ) or (9.10 * quantity / 1000.0)
            if hasattr(tg_notifier, "calculate_profit"):
                profit_rub = tg_notifier.calculate_profit(order_price_rub=price_rub, usdt_cost=charge)
            else:
                profit_rub = 0.0

            await db.save_order_status(
                order_id=order_id,
                username=username,
                status="COMPLETED",
                quantity=quantity,
                price_rub=price_rub if price_rub else None,
                cost_usdt=round(charge, 4),
                profit_rub=profit_rub,
                gameau_order_id=gameau_id,
            )
            await tracker.log(order_id, EV_GAMEAU_COMPLETED, f"via reconcile: {status}")
            await tracker.log(order_id, EV_COMPLETED, f"profit={profit_rub}₽")

            formatted_quantity = f"{quantity:,}".replace(",", " ")
            reply_text = (
                f"✅ Здравствуйте! {formatted_quantity} Telegram Stars успешно зачислены на аккаунт @{username}. "
                f"Пожалуйста, подтвердите успешное выполнение заказа на FunPay! Спасибо за покупку!"
            )
            try:
                if await funpay_client.send_message(node=order.get("chat_node") or order_id, text=reply_text):
                    await tracker.log(order_id, EV_FUNPAY_REPLY, "via reconcile")
            except Exception as exc:
                logger.warning(f"[RECONCILE] Не удалось ответить покупателю #{order_id}: {exc}")

            try:
                if hasattr(tg_notifier, "alert_order_completed"):
                    await tg_notifier.alert_order_completed(
                        order_id=order_id,
                        username=username or "?",
                        profit_rub=profit_rub,
                        order_price_rub=price_rub,
                        usdt_cost=charge,
                    )
                    await tracker.log(order_id, EV_TG_ALERT, "reconcile completed")
            except Exception as exc:
                logger.warning(f"[RECONCILE] Не удалось отправить алерт: {exc}")

            logger.info(f"[RECONCILE] Заказ #{order_id} досинхронизирован: COMPLETED")
            finished += 1

        elif status in FAILED_STATUSES:
            await db.save_order_status(
                order_id=order_id,
                username=username,
                status="FAILED_DELIVERY",
                quantity=quantity,
                error=f"GAMEAU_STATUS_{status}",
                gameau_order_id=gameau_id,
            )
            await tracker.log(order_id, EV_GAMEAU_FAILED, f"via reconcile: {status}")
            await tracker.log(order_id, EV_FAILED, f"GAMEAU_STATUS_{status}")
            try:
                await tg_notifier.send_alert(
                    f"❌ <b>ЗАДача не выполнена:</b> GAMEAU отклонил заказ {gameau_id} "
                    f"для FunPay #{order_id} ({status}). Потребует ручной разбор."
                )
                await tracker.log(order_id, EV_TG_ALERT, "reconcile failed")
            except Exception as exc:
                logger.warning(f"[RECONCILE] Не удалось отправить алерт об ошибке: {exc}")
            finished += 1

    if finished:
        logger.info(f"[RECONCILE] В этом проходе завершено задач: {finished}")
    return finished
