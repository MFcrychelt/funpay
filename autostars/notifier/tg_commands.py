"""Интерактивный Telegram-бот для управления AutoStars (v2.2).

Long-polling getUpdates (вебхук-сервер не нужен). Команды доступны только
владельцу (chat_id из TELEGRAM_CHAT_ID).

Команды:
  /help, /status, /stats, /report, /tasks, /balance,
  /pause, /resume, /retry <order_id>, /calc <цена ₽> [звёзды],
  /limits, /held, /release <order_id>, /blacklist [add|remove @nick], /customers [дней]

Права: команды принимает только владелец чата из TELEGRAM_CHAT_ID (`_authorized`).
`/release` — единственная команда, которая тратит деньги «в обход» политики выдачи:
она осознанно ручная и работает только для статуса HOLD_MANUAL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .tg_alert import TelegramNotifier

logger = logging.getLogger("autostars.tg_commands")

Handler = Callable[[str], Awaitable[str | None]]

HELP_TEXT = (
    "🤖 <b>AutoStars — управление</b>\n\n"
    "/help — список команд\n"
    "/status — статус бота (пауза, баланс, задачи)\n"
    "/stats — статистика 1ч/24ч/сегодня/всего\n"
    "/report — полный отчёт (P&L, убытки, топ)\n"
    "/tasks — трекинг задач (открытые/зависшие)\n"
    "/balance — баланс GAMEAU\n"
    "/pause — пауза приёма новых заказов\n"
    "/resume — возобновить выдачу\n"
    "/retry &lt;id заказа&gt; — повторить проваленный заказ\n"
    "/calc &lt;цена ₽&gt; [звёзды] — калькулятор прибыли\n"
    "/limits — лимиты выдачи и сколько потрачено сегодня\n"
    "/held — заказы, задержанные политикой (ждут решения)\n"
    "/release &lt;id&gt; — отпустить задержанный заказ (покупает звёзды!)\n"
    "/blacklist — стоп-лист; /blacklist add @nick [NOTE] | remove @nick\n"
    "/customers [дней] — топ покупателей за период\n"
    "/stop — корректно остановить цикл (текущие выдачи дождёмся)\n\n"
    "Пример: /retry 1234567890"
)


class TelegramCommandServer:
    """Фоновый long-poll обработчик команд владельца в Telegram."""

    def __init__(self, notifier: TelegramNotifier, handlers: dict[str, Handler]):
        self.notifier = notifier
        self.handlers = handlers
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return self.notifier.is_configured

    def _authorized(self, chat_id: Any) -> bool:
        """Только владелец (chat_id из конфигурации)."""
        return str(chat_id) == str(self.notifier.chat_id)

    def parse_command(self, text: str) -> tuple[str, str]:
        """' /stats extra ' → ('stats', 'extra'). Нераспознанное → ('', text)."""
        if not text or not text.startswith("/"):
            return "", text or ""
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0][1:].split("@")[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        return cmd, args

    async def dispatch(self, text: str) -> str | None:
        """Возвращает текст ответа на команду (None — молчим)."""
        cmd, args = self.parse_command(text)
        if not cmd:
            return None
        handler = self.handlers.get(cmd)
        if handler is None:
            return f"❓ Неизвестная команда «/{cmd}».\n{HELP_TEXT}"
        try:
            return await handler(args)
        except Exception as exc:
            logger.error(f"Ошибка обработчика /{cmd}: {exc}", exc_info=True)
            return f"⚠️ Ошибка при выполнении /{cmd}: {exc}"

    async def _get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=65.0)
        url = f"https://api.telegram.org/bot{self.notifier.bot_token}/getUpdates"
        params: dict[str, Any] = {
            "timeout": 50,  # long polling — реакции на команды почти мгновенные
            "allowed_updates": '["message"]',
        }
        if offset is not None:
            params["offset"] = offset
        response = await self._client.get(url, params=params)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates: {data}")
        return data.get("result", [])

    async def run(self, stop_event: asyncio.Event) -> None:
        """Основной цикл: long-poll → авторизация → dispatch → ответ."""
        if not self.enabled:
            logger.info("TG-команды: не настроен (токен/chat_id пуст) — отключено")
            return

        offset: int | None = None
        logger.info("TG-команды: сервер запущен (long polling)")
        while not stop_event.is_set():
            try:
                updates = await self._get_updates(offset)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"TG getUpdates ошибка: {exc}, пауза 10s")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    pass
                continue

            for upd in updates:
                offset = upd["update_id"] + 1
                message = upd.get("message") or {}
                text = message.get("text") or ""
                chat = message.get("chat") or {}

                if not self._authorized(chat.get("id")):
                    logger.info(
                        f"TG: проигнорировано сообщение от чата {chat.get('id')} (не владелец)"
                    )
                    continue

                reply = await self.dispatch(text)
                if not reply:
                    continue

                # Длинные отчёты разбиваем на части (лимит Telegram 4096)
                try:
                    await self.notifier.send_stats_report(reply)
                except Exception as exc:
                    logger.error(f"TG: не удалось отправить ответ: {exc}")

        await self.close()

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


def build_default_handlers(
    cfg: Any,
    db: Any,
    gameau_client: Any,
    notifier: TelegramNotifier,
    stats: Any,
    tracker: Any,
    loop_controls: dict[str, Any] | None = None,
) -> dict[str, Handler]:
    """
    Собирает стандартные обработчики команд.
    loop_controls — {"funpay": client, "gameau": client, "notify": notifier,
    "paused": {"value": bool}} если нужен реальный /pause /resume /retry
    в живом цикле.
    """
    from ..services import bot_control
    from ..services.policy import format_username

    async def h_status(_args: str) -> str | None:
        paused = await bot_control.is_bot_paused(db)
        counts = await tracker.summary()
        balance = await bot_control.check_gameau_balance(
            gameau_client, notifier, cfg.low_balance_threshold_usdt, alert=False
        )
        lines = [
            "🤖 <b>AUTOSTARS — статус</b>",
            f"{'⏸ В ПАУЗЕ' if paused else '🟢 РАБОТАЕТ'}",
            "Задачи: " + (", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "нет"),
        ]
        if balance:
            lines.append(
                f"Баланс GAMEAU: {balance['balance']:.2f} {balance['currency']} "
                f"(тариф {balance['plan']})"
            )
        return "\n".join(lines)

    async def h_stats(_args: str) -> str | None:
        windows = [
            await stats.window("1 час", 1),
            await stats.day_window(),
            await stats.today_window(),
            await stats.total_window(),
        ]
        return stats.render_telegram(windows)

    async def h_report(_args: str) -> str | None:
        windows = await stats.compute_all()
        import time as _t

        now = int(_t.time())
        top = await db.top_orders(limit=5, start_ts=now - 86400, end_ts=now)
        failed = await db.top_orders(limit=5, start_ts=now - 86400, end_ts=now, failed_only=True)
        negative = await db.get_negative_profit_orders(limit=5, start_ts=now - 86400, end_ts=now)
        report = stats.render_full_report(windows, top, failed, await tracker.summary())
        if negative:
            report += "\n🔻 Убыточные сделки за 24ч (маржа < 0):\n"
            for o in negative:
                report += (
                    f"   #{o['order_id']} @{o.get('username') or '?'} — "
                    f"убыток {float(o.get('profit_rub') or 0):.2f} ₽\n"
                )
        return report

    async def h_tasks(_args: str) -> str | None:
        text = await tracker.render_text(stuck_minutes=cfg.stuck_task_minutes)
        # Убираем рамку для TG
        return text.replace("=", "").replace("-", "").strip()

    async def h_balance(_args: str) -> str | None:
        info = await bot_control.check_gameau_balance(
            gameau_client, notifier, cfg.low_balance_threshold_usdt, alert=True
        )
        if info is None:
            return "⚠️ Не удалось получить баланс GAMEAU (проверьте ключ/сеть)."
        emoji = "🚨" if info["balance"] < cfg.low_balance_threshold_usdt else "💰"
        return (
            f"{emoji} <b>Баланс GAMEAU:</b> {info['balance']:.2f} {info['currency']}\n"
            f"Тариф: {info['plan']} | Статус: {info['status']}\n"
            f"Порог алерта: {cfg.low_balance_threshold_usdt:.2f}"
        )

    async def h_pause(_args: str) -> str | None:
        if loop_controls and "set_paused" in loop_controls:
            await loop_controls["set_paused"](True)
        else:
            await bot_control.set_bot_paused(db, True)
        return "⏸ <b>ПРИЁМ НОВЫХ ЗАКАЗОВ ОСТАНОВЛЕН.</b>\nВ работе уже принятые заказы " \
               "и reconciliation продолжаются. /resume — вернуть в работу."

    async def h_resume(_args: str) -> str | None:
        if loop_controls and "set_paused" in loop_controls:
            await loop_controls["set_paused"](False)
        else:
            await bot_control.set_bot_paused(db, False)
        return "🟢 <b>ВЫДАЧА ВОЗОБНОВЛЕНА.</b> Новые заказы FunPay снова обрабатываются автоматически."

    async def h_retry(args: str) -> str | None:
        order_id = args.strip()
        if not order_id:
            return "Использование: /retry <id заказа>\nНапример: /retry 1234567890"
        lc = loop_controls or {}
        funpay = lc.get("funpay")
        if funpay is None:
            return "⚠️ Ретрай доступен только при работающем цикле автовыдачи."
        res = await bot_control.retry_failed_order(
            order_id=order_id,
            db=db,
            funpay_client=funpay,
            gameau_client=gameau_client,
            tg_notifier=notifier,
            tracker=tracker,
            default_quantity=cfg.default_stars_quantity,
            max_charge_usdt=cfg.default_max_charge_usdt,
            whitebird_rate=cfg.active_usdt_rate,
            hide_sender=cfg.hide_sender,
            max_order_retries=cfg.max_order_retries,
            wait_completion_timeout=cfg.wait_completion_timeout,
            usdt_per_1000_stars=cfg.usdt_per_1000_stars,
            max_charge_margin_pct=cfg.max_charge_margin_pct,
        )
        status = res.get("status")
        if status == "completed":
            return f"✅ <b>Заказ #{order_id} выполнен повторно!</b> " \
                   f"Прибыль: {res.get('profit_rub', 0):.2f} ₽"
        if status in ("failed",):
            return f"❌ Повтор не удался: {res.get('error')}\nПроверьте баланс GAMEAU и попробуйте снова."
        if status == "not_found":
            return f"❓ Заказ #{order_id} не найден в базе."
        if status == "not_retryable":
            return f"⛔ Заказ #{order_id} в статусе {res.get('current_status')} — ретрай недоступен."
        if status == "no_username":
            return f"⏳ Заказ #{order_id}: нет @username, ждём ответ покупателя."
        if status == "unconfirmed":
            return f"⏳ Заказ #{order_id} отправлен в GAMEAU, ждём подтверждения статуса."
        if status == "waiting_username":
            return f"⏳ Заказ #{order_id}: покупателю отправлен запрос @username."
        return f"Результат: {res}"


    async def h_limits(_args: str) -> str | None:
        pol = cfg.order_policy()
        spent = 0.0
        with contextlib.suppress(Exception):
            spent = await db.sum_cost_usdt_since(pol.day_start_ts())
        counts = await tracker.summary()
        held = int(counts.get("HOLD_MANUAL", 0))
        lines = [
            "🛡 <b>Политика выдачи</b>",
            f"Лимит сделки: {pol.max_order_revenue_rub:.0f} ₽" if pol.max_order_revenue_rub else "Лимит сделки: выключен",
            f"Мин. маржа: {pol.min_margin_pct:.1f}%" if pol.min_margin_pct else "Мин. маржа: выключена",
            f"Суточный бюджет: {pol.daily_spend_limit_usdt:.2f} USDT" if pol.daily_spend_limit_usdt
            else "Суточный бюджет: выключен",
            f"Дубли: окно {pol.duplicate_window_min} мин → {pol.duplicate_action}" if pol.duplicate_window_min
            else "Дубли: не проверяются",
            f"Стоп-лист: {'вкл' if pol.blacklist_enabled else 'выкл'} "
            f"({len(await _flag_list(db))} ник.)",
            f"Потрачено с полуночи: {spent:.2f} USDT",
            f"Задержано заказов: {held}" + (" — /held" if held else ""),
        ]
        return "\n".join(lines)

    async def h_held(_args: str) -> str | None:
        rows = await db.get_orders_by_status(("HOLD_MANUAL",), limit=10)
        if not rows:
            return "✅ Задержанных заказов нет."
        lines = ["⛔ <b>Ждут решения:</b>"]
        for row in rows:
            oid = row.get("order_id")
            lines.append(
                f"#{oid} {format_username(row.get('username'))} • {row.get('quantity')}⭐ • {row.get('price_rub')} ₽\n"
                f"   {str(row.get('error') or '').strip() or 'без причины'}\n"
                f"   /release {oid}  •  отказать: `--cancel {oid} -y` в консоли"
            )
        return "\n".join(lines)

    async def h_release(args: str) -> str | None:
        order_id = args.strip()
        if not order_id:
            return "Использование: /release <id заказа>\nСписок задержанных: /held"
        row = await db.get_order(order_id)
        if row is None:
            return f"❓ Заказ #{order_id} не найден."
        if row.get("status") != "HOLD_MANUAL":
            return f"⛔ #{order_id} не задержан (статус {row.get('status')}). Для провалов — /retry {order_id}"
        if not row.get("username"):
            return f"⏳ #{order_id}: нет @username — выдача невозможна, ждём ответа покупателя."
        lc = loop_controls or {}
        funpay = lc.get("funpay")
        if funpay is None:
            return "⚠️ Отпустить заказ можно только при работающем цикле (/release в CLI: `--release`)."
        res = await bot_control.retry_failed_order(
            order_id=order_id,
            db=db,
            funpay_client=funpay,
            gameau_client=gameau_client,
            tg_notifier=notifier,
            tracker=tracker,
            policy=cfg.order_policy(),
            ignore_policy=True,
            default_quantity=cfg.default_stars_quantity,
            max_charge_usdt=cfg.default_max_charge_usdt,
            whitebird_rate=cfg.active_usdt_rate,
            hide_sender=cfg.hide_sender,
            max_order_retries=cfg.max_order_retries,
            wait_completion_timeout=cfg.wait_completion_timeout,
            usdt_per_1000_stars=cfg.usdt_per_1000_stars,
            max_charge_margin_pct=cfg.max_charge_margin_pct,
        )
        updated = await db.get_order(order_id)
        state = (updated or {}).get("status")
        if state == "COMPLETED":
            return (f"✅ #{order_id} выдан: @{str(row.get('username')).lstrip('@')} × {row.get('quantity')}⭐, "
                    f"прибыль {(updated or {}).get('profit_rub')} ₽")
        return (f"⚠️ #{order_id}: результат {res.get('status')}, статус {state}. "
                f"{str((updated or {}).get('error') or '')[:200]}")

    async def h_blacklist(args: str) -> str | None:
        parts = args.split(maxsplit=2)
        action = parts[0].lower() if parts else "list"
        nick = parts[1].lstrip("@") if len(parts) > 1 else ""
        note = parts[2] if len(parts) > 2 else ""
        if action == "add" and nick:
            await db.add_buyer_flag(nick, note=note or None)
            return f"✅ @{nick} в стоп-листе" + (f" ({note})" if note else "") + ". Его заказы будут задерживаться."
        if action in ("remove", "rm") and nick:
            removed = await db.remove_buyer_flag(nick)
            return f"✅ @{nick} убран из стоп-листа" if removed else f"@{nick} в стоп-листе не значится."
        rows = await _flag_list(db)
        if not rows:
            return "Стоп-лист пуст. /blacklist add @nick причина"
        return "🚫 <b>Стоп-лист:</b>\n" + "\n".join(
            f"• {format_username(r.get('username'))}"
            + (f" — {r.get('note')}" if r.get("note") else "")
            for r in rows
        )

    async def h_customers(args: str) -> str | None:
        try:
            days = float(args) if args.strip() else 7.0
        except ValueError:
            return "Использование: /customers [дней]\nНапример: /customers 30"
        since = int(time.time() - max(days, 1 / 24) * 86400)
        rows = await db.buyer_stats(since, limit=8)
        if not rows:
            return f"За {days:g} суток заказов не было."
        lines = [f"👥 <b>Покупатели за {days:g} сут. (топ-8 по обороту):</b>"]
        for row in rows:
            lines.append(
                f"{format_username(row.get('username'))}: {row['orders']} зак. / {row['completed']} готово"
                f"{' / ' + str(row['failed']) + ' провал.' if row.get('failed') else ''}"
                f" • {float(row['revenue_rub']):.0f} ₽ → +{float(row['profit_rub']):.0f} ₽"
            )
        return "\n".join(lines)

    async def h_stop(_args: str) -> str | None:
        """Корректная остановка цикла прямо из Telegram (graceful shutdown)."""
        if not loop_controls or "stop" not in loop_controls:
            return "⚠️ Остановка из Telegram доступна только в живом цикле (python -m autostars.main)."
        await loop_controls["stop"]()
        return "🛑 <b>Останавливаюсь корректно:</b> дождусь текущих выдач и закрою соединения."

    async def h_calc(args: str) -> str | None:
        parts = args.split()
        try:
            price = float(parts[0].replace(",", ".")) if parts else 1370.4
            stars = int(parts[1]) if len(parts) > 1 else 1000
        except (ValueError, IndexError):
            return "Использование: /calc <цена ₽> [звёзды]\nНапример: /calc 1370.40 1000"
        usdt_cost = round(cfg.estimate_cost_usdt(stars), 4)
        summary = notifier.get_profit_summary(price, usdt_cost)
        return (
            f"🧮 <b>Калькулятор — {stars} Stars</b>\n"
            f"Выручка: {price:.2f} ₽ | Затраты: {usdt_cost:.2f} USDT\n\n"
            f"Вариант 2 (Whitebird {cfg.rate_variant_2:.2f} ₽): "
            f"себестоимость {summary['variant_2']['cost_rub']:.2f} ₽ → "
            f"прибыль <b>+{summary['variant_2']['profit_rub']:.2f} ₽</b>\n"
            f"Вариант 1 (анонимный {cfg.rate_variant_1:.2f} ₽): "
            f"себестоимость {summary['variant_1']['cost_rub']:.2f} ₽ → "
            f"прибыль <b>+{summary['variant_1']['profit_rub']:.2f} ₽</b>"
        )

    return {
        "help": lambda _a: _help(),
        "status": h_status,
        "stats": h_stats,
        "report": h_report,
        "tasks": h_tasks,
        "balance": h_balance,
        "pause": h_pause,
        "resume": h_resume,
        "retry": h_retry,
        "calc": h_calc,
        "limits": h_limits,
        "held": h_held,
        "release": h_release,
        "blacklist": h_blacklist,
        "customers": h_customers,
        "stop": h_stop,
    }


async def _help() -> str:
    return HELP_TEXT


async def _flag_list(db: Any) -> list[dict[str, Any]]:
    """Стоп-лист: тестовые заглушки db могут не иметь этого метода."""
    try:
        return list(await db.list_buyer_flags("blacklist"))
    except Exception:
        return []
