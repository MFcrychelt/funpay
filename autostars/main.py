"""Точка входа AutoStars (CLI + главный цикл автовыдачи).

    python -m autostars.main                     # цикл автовыдачи (polling + reconcile)
    python -m autostars.main --once              # однократный проход (для cron)
    python -m autostars.main --check             # диагностика FunPay/GAMEAU/БД/TG/конфига
    python -m autostars.main --catalog           # каталог GAMEAU с себестоимостью по обоим курсам
    python -m autostars.main --calc 1370.4 1000  # калькулятор прибыли (Вариант 1 vs 2)
    python -m autostars.main --deposit-info      # инструкция по заводу USDT TRC-20
    python -m autostars.main --config-show       # активная конфигурация (секреты маскируются)
    python -m autostars.main --test-order durov  # тестовый заказ (--dry-run сначала!)
    python -m autostars.main --stats             # статистика 1/2/3/4/6/24ч, день, всего (--json)
    python -m autostars.main --report            # P&L, убытки, топ сделок, провалы
    python -m autostars.main --tasks              # трекинг задач (открытые/зависшие)
    python -m autostars.main --timeline 123       # жизненный цикл одной задачи
    python -m autostars.main --balance            # баланс GAMEAU и «на сколько заказов хватит»
    python -m autostars.main --pause / --resume   # пауза/возобновление приёма заказов
    python -m autostars.main --retry-order 123    # повтор проваленного заказа (идемпотентно)
    python -m autostars.main --limits             # политика выдачи и расход с полуночи
    python -m autostars.main --held               # задержанные заказы (ждут решения)
    python -m autostars.main --release 123 -y      # отпустить задержанный заказ
    python -m autostars.main --cancel 123 -y       # отклонить задержанный заказ
    python -m autostars.main --order @nick 1000     # ручная выдача вне сделки FunPay
    python -m autostars.main --blacklist add @nick причина   # стоп-лист покупателей
    python -m autostars.main --customers --days 30 # покупатели за период
    python -m autostars.main --templates            # шаблоны ответов покупателю
    python -m autostars.main --export out.csv --since 7d     # выгрузка истории
    python -m autostars.main --metrics              # снимок в формате Prometheus

В живом цикле доступны TG-команды владельца: /help /status /stats /report /tasks
/balance /pause /resume /retry <id> /calc <цена> [звёзды] /limits /held
/release <id> /blacklist /customers.

Что добавлено в v2.4 (кроме hygiene-ревизии 2.3):
  • политика выдачи — лимит сделки, минимальная маржа, суточный бюджет USDT, поиск
    дублей и стоп-лист: всё считается ДО покупки, спорный заказ уходит в `HOLD_MANUAL`
    (не в `FAILED`: оплата уже есть) и ждёт `--held` + `--release`/`--cancel`;
  • шаблоны ответов покупателю в конфиге (REPLY_*), «тихие часы» для алертов;
  • ручная выдача (--order), выгрузка истории (--export csv/json), --customers;
  • /metrics + /healthz + /status (по умолчанию выключено, слушает localhost).

Что исправлено в v2.3:
  • остановка по SIGTERM/SIGINT корректно дожидается задач в работе (graceful
    shutdown) — раньше systemd-`stop` рвал посреди покупки в GAMEAU;
  • недоступный FunPay при старте больше не убивает процесс: логин повторяется
    с backoff (STARTUP_MAX_RETRIES=0 — вечно, под systemd Restart=always);
  • себестоимость берётся из каталога GAMEAU, а не из «9.10 USDT за 1000⭐»;
  • лимит maxCharge считается по пакету, поэтому крупные заказы (2000⭐+) не
    отваливаются с PRICE_EXCEEDED;
  • `--test-order` тратит реальные деньги только после подтверждения (--yes);
  • параллельная выдача ограничена семафором, LONG-POLL не блокирует таймеры цикла;
  • `--check` проверяет конфигурацию и не печатает csrf-токен;
  • есть `--json` (GUI/дашборды) и `--config-show`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import __version__
from .clients.funpay import FunPayClient, FunPayError
from .clients.gameau import GameauClient
from .config import Config, ensure_env_file
from .database.db_manager import DBManager
from .notifier.tg_alert import TelegramNotifier
from .notifier.tg_commands import TelegramCommandServer, build_default_handlers
from .security import mask_secret
from .services import bot_control
from .services.order_processor import process_paid_order
from .services.policy import evaluate_order_risk, format_username
from .services.statistics import StatisticsService
from .services.task_tracker import TaskTracker, reconcile_inflight_orders

logger = logging.getLogger("autostars")

Printer = Callable[[str], None]


# ============================================================================ #
# Логирование и конструирование клиентов
# ============================================================================ #

def setup_logging(verbose: bool = False, cfg: Config | None = None) -> None:
    """Консоль + ротируемый файл. Каталог для файла создаётся, если его нет."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    # stderr, а не stdout: `--stats --json > report.json` и перенаправления
    # не должны получать лог-строки вперемешку с данными
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    log_file = cfg.log_file if cfg else None
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    log_file,
                    maxBytes=cfg.log_max_bytes if cfg else 5_000_000,
                    backupCount=cfg.log_backup_count if cfg else 5,
                    encoding="utf-8",
                )
            )
        except OSError as exc:
            print(f"[WARN] Лог в файл {log_file} не пишется: {exc}", file=sys.stderr)

    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    # aiohttp/httpx пишут в root на INFO — глушим до WARNING, stays читаемо
    for noisy in ("httpx", "httpcore", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_funpay(cfg: Config) -> FunPayClient:
    return FunPayClient(
        golden_key=cfg.golden_key,
        user_agent=cfg.funpay_user_agent,
        base_url=cfg.funpay_base_url,
        timeout=cfg.gameau_timeout,
    )


def build_gameau(cfg: Config) -> GameauClient:
    return GameauClient(
        api_key=cfg.gameau_key,
        base_url=cfg.gameau_base_url,
        max_retries=cfg.gameau_max_retries,
        timeout=cfg.gameau_timeout,
    )


def build_notifier(cfg: Config) -> TelegramNotifier:
    return TelegramNotifier(
        bot_token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        rate_variant_1=cfg.rate_variant_1,
        rate_variant_2=cfg.rate_variant_2,
        active_variant=cfg.exchange_variant,
        tron_energy_fee_rub=cfg.tron_energy_fee_rub,
        mute_window=cfg.mute_window(),
    )


def build_statistics(db: DBManager, cfg: Config) -> StatisticsService:
    return StatisticsService(
        db=db,
        rate_variant_1=cfg.rate_variant_1,
        rate_variant_2=cfg.rate_variant_2,
        active_variant=cfg.exchange_variant,
        tron_energy_fee_rub=cfg.tron_energy_fee_rub,
    )


# обратная совместимость (импортировали как приватные)
_build_statistics = build_statistics
_build_notifier = build_notifier


@asynccontextmanager
async def open_db(cfg: Config) -> AsyncIterator[DBManager]:
    """БД с гарантированным закрытием (иначе aiosqlite-поток держит процесс)."""
    db = DBManager(cfg.db_path)
    await db.init_db()
    try:
        yield db
    finally:
        await db.close()


def _order_kwargs(cfg: Config) -> dict[str, Any]:
    """Единый набор параметров пайплайна (run_bot, retry, чат-мониторинг, GUI)."""
    return {
        "default_quantity": cfg.default_stars_quantity,
        "max_charge_usdt": cfg.default_max_charge_usdt,
        "whitebird_rate": cfg.active_usdt_rate,
        "hide_sender": cfg.hide_sender,
        "max_order_retries": cfg.max_order_retries,
        "wait_completion_timeout": cfg.wait_completion_timeout,
        "usdt_per_1000_stars": cfg.usdt_per_1000_stars,
        "max_charge_margin_pct": cfg.max_charge_margin_pct,
        "policy": cfg.order_policy(),
    }


def _emit(printer: Printer, text: str = "") -> None:
    printer(text)


# ============================================================================ #
# Отчёты (консоль / JSON для GUI)
# ============================================================================ #

async def cmd_stats(cfg: Config, hours: list[float] | None = None, as_json: bool = False) -> int:
    """Статистика по окнам 1ч/2ч/3ч/4ч/6ч/24ч + календарный день + всего."""
    async with open_db(cfg) as db:
        stats = build_statistics(db, cfg)
        if hours:
            windows = [await stats.window(f"последние {h:g} ч", h) for h in hours]
            windows.append(await stats.today_window())
        else:
            windows = await stats.compute_all()
        print(stats.render_json(windows) if as_json else stats.render_text(windows))
        return 0


async def cmd_report(cfg: Config, as_json: bool = False) -> int:
    """Полный отчёт: окна + P&L + убыточные сделки + топ + провалы + трекинг."""
    async with open_db(cfg) as db:
        stats = build_statistics(db, cfg)
        windows = await stats.compute_all()
        now = int(time.time())
        day_ago = now - 86400
        top = await db.top_orders(limit=5, start_ts=day_ago, end_ts=now)
        failed = await db.top_orders(limit=10, start_ts=day_ago, end_ts=now, failed_only=True)
        negative = await db.get_negative_profit_orders(limit=10, start_ts=day_ago, end_ts=now)
        status_counts = await db.get_status_counts()

        if as_json:
            payload = {
                "generated_at": now,
                "windows": [w.to_dict() for w in windows],
                "status_counts": status_counts,
                "top_orders": top,
                "failed_orders": failed,
                "negative_orders": negative,
            }
            print(json.dumps(payload, ensure_ascii=False, default=str))
            return 0

        report = stats.render_full_report(windows, top, failed, status_counts)
        if negative:
            report += "\n🔻 Убыточные сделки за 24ч (маржа < 0):\n"
            for order in negative:
                report += (
                    f"   #{order['order_id']} @{order.get('username') or '?'} — "
                    f"выручка {float(order.get('price_rub') or 0):.2f} ₽, "
                    f"затраты {float(order.get('cost_usdt') or 0):.2f} USDT → "
                    f"убыток {float(order.get('profit_rub') or 0):.2f} ₽\n"
                )
            report += "\n   → проверьте цену лота на FunPay или курс закупки USDT\n"
        print(report)
        return 0


async def cmd_tasks(cfg: Config, as_json: bool = False) -> int:
    """Трекинг выполнения задач: открытые, зависшие, журнал событий."""
    async with open_db(cfg) as db:
        tracker = TaskTracker(db)
        if as_json:
            print(
                json.dumps(
                    {
                        "summary": await tracker.summary(),
                        "open": await tracker.open_tasks(limit=50),
                        "stuck": await tracker.stuck_tasks(cfg.stuck_task_minutes),
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
            return 0
        print(await tracker.render_text(stuck_minutes=cfg.stuck_task_minutes))
        return 0


async def cmd_timeline(cfg: Config, order_id: str) -> int:
    """Жизненный цикл одной задачи по ID заказа FunPay."""
    async with open_db(cfg) as db:
        tracker = TaskTracker(db)
        order = await db.get_order(order_id)
        if order is None:
            print(f"Заказ #{order_id} не найден в базе.")
            return 1
        print(f"\n📋 ЗАДАЧА #{order['order_id']} — {order['status']}")
        print(
            f"   Получатель: @{order.get('username') or 'не указан'} | "
            f"Звёзды: {order.get('quantity')} | Цена: {order.get('price_rub')} ₽ | "
            f"Затраты: {order.get('cost_usdt')} USDT | Прибыль: {order.get('profit_rub')} ₽"
        )
        print(f"   GAMEAU ID: {order.get('gameau_order_id') or '—'} | Ошибка: {order.get('error') or '—'}")
        print(f"   Создан: {order.get('created_at')} | Обновлён: {order.get('updated_at')}\n")

        events = await tracker.timeline(order_id)
        if not events:
            print("   Событий в журнале нет (заказ создан до v2.1).")
        else:
            print("Журнал выполнения:")
            for event in events:
                ts = time.strftime("%d.%m %H:%M:%S", time.localtime(event.get("ts_epoch") or 0))
                print(f"   {ts} | {event['event']:<18} | {event.get('detail') or ''}")
        print()
        return 0


async def cmd_balance(cfg: Config, as_json: bool = False) -> int:
    """Баланс GAMEAU и прогноз «на сколько заказов 1000⭐ хватит»."""
    gameau = build_gameau(cfg)
    try:
        info = await bot_control.check_gameau_balance(gameau, build_notifier(cfg), cfg.low_balance_threshold_usdt, alert=False)
        if info is None:
            print("[ERR] Не удалось получить баланс GAMEAU (проверьте GAMEAU_API_KEY и сеть).")
            return 1
        cost_1000_usdt = 0.0
        with contextlib.suppress(Exception):
            cost_1000_usdt = await gameau.price_for_stars(1000, cfg.usdt_per_1000_stars)
        cost_1000_usdt = cost_1000_usdt or cfg.usdt_per_1000_stars
        can_pay = int(info["balance"] // cost_1000_usdt) if info["balance"] > 0 and cost_1000_usdt else 0

        if as_json:
            print(
                json.dumps(
                    {
                        **info,
                        "threshold_usdt": cfg.low_balance_threshold_usdt,
                        "cost_1000_usdt": round(cost_1000_usdt, 4),
                        "orders_1000_affordable": can_pay,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
            return 0

        emoji = "🚨" if info["balance"] < cfg.low_balance_threshold_usdt else "💰"
        print(
            f"{emoji} Баланс GAMEAU: {info['balance']:.2f} {info['currency']} "
            f"(тариф {info['plan']}, статус {info['status']})"
        )
        print(f"   Порог алерта: {cfg.low_balance_threshold_usdt:.2f} {info['currency']}")
        print(
            f"   Хватает на: ~{can_pay} заказ(ов) по 1000⭐ "
            f"({cost_1000_usdt:.2f} USDT ≈ {cost_1000_usdt * cfg.active_usdt_rate:.2f} ₽ по активному курсу)"
        )
        return 0
    finally:
        await gameau.close()


async def cmd_pause(cfg: Config, pause: bool) -> int:
    """Пауза/возобновление приёма новых заказов (флаг в БД, переживает рестарт)."""
    async with open_db(cfg) as db:
        await bot_control.set_bot_paused(db, pause)
        print(
            "⏸ ПРИЁМ НОВЫХ ЗАКАЗОВ ОСТАНОВЛЕН. В работе остаются принятые заказы и reconciliation."
            if pause
            else "🟢 ВЫДАЧА ВОЗОБНОВЛЕНА. Новые заказы FunPay снова обрабатываются."
        )
        return 0


async def cmd_retry(cfg: Config, order_id: str) -> int:
    """Повтор проваленного заказа (идемпотентно: дубль покупки невозможен)."""
    cfg.validate()
    async with open_db(cfg) as db:
        funpay, gameau, notifier = build_funpay(cfg), build_gameau(cfg), build_notifier(cfg)
        try:
            row = await db.get_order(order_id)
            if row is None:
                print(f"[ERR] Заказ #{order_id} не найден в базе.")
                return 1
            print(
                f"Ретрай заказа #{order_id} (текущий статус: {row.get('status')}, "
                f"цена: {row.get('price_rub')} ₽, затраты: {row.get('cost_usdt')} USDT)"
            )
            result = await bot_control.retry_failed_order(
                order_id=order_id,
                db=db,
                funpay_client=funpay,
                gameau_client=gameau,
                tg_notifier=notifier,
                tracker=TaskTracker(db),
                **_order_kwargs(cfg),
            )
            error = result.get("error")
            print(f"Результат: {result.get('status')}" + (f" ({error})" if error else ""))
            updated = await db.get_order(order_id)
            if updated:
                print(
                    f"Новый статус: {updated['status']} (затраты {updated.get('cost_usdt')} USDT, "
                    f"прибыль {updated.get('profit_rub')} ₽)"
                )
            return 0 if result.get("status") in ("completed", "unconfirmed") else 1
        finally:
            await funpay.close()
            await gameau.close()
            await notifier.close()


# ============================================================================ #
# Ручная выдача, лимиты, стоп-лист, выгрузка
# ============================================================================ #

def _confirm_or_exit(assume_yes: bool, question: str, code: int = 2) -> bool:
    """Подтверждение «рука оператора»: без `-y` в неинтерактивном режиме — отказ."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"\n❌ {question} Подтвердите действие флагом -y.")
        return False
    answer = input(f"\n{question} [y/N] ").strip().lower()
    return answer in ("y", "yes", "да")


async def cmd_held(cfg: Config, as_json: bool = False, limit: int = 20) -> int:
    """Список заказов, задержанных политикой выдачи (ждут решения человека)."""
    async with open_db(cfg) as db:
        rows = await TaskTracker(db).held_orders(limit=limit)
        if as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            return 0
        if not rows:
            print("✅ Задержанных заказов нет — политика выдачи чиста.")
            return 0
        print(f"⛔ Задержано политикой выдачи: {len(rows)} (ждут решения)\n")
        for row in rows:
            reason = str(row.get("error") or row.get("error_message") or "").strip() or "без причины"
            print(
                f"  • #{row.get('order_id')} {format_username(row.get('username'))} "
                f"{row.get('quantity')}⭐ за {row.get('price_rub')} ₽ — {reason}\n"
                f"    отпустить: autostars --release {row.get('order_id')} -y"
            )
        return 0


async def cmd_release(cfg: Config, order_id: str, assume_yes: bool = False, as_json: bool = False) -> int:
    """Отпускает задержанный заказ: выдаёт его в обход политики (решение человека)."""
    cfg.validate()
    async with open_db(cfg) as db:
        row = await db.get_order(order_id)
        if row is None:
            print(f"[ERR] Заказ #{order_id} не найден в базе.")
            return 1
        status = row.get("status")
        if status != "HOLD_MANUAL":
            print(f"❌ #{order_id} сейчас в статусе {status} — освобождать нечего. "
                  "Для проваленных заказов есть --retry-order.")
            return 1
        if not row.get("username"):
            print("❌ У заказа нет @username — выдача невозможна: дождитесь ответа покупателя "
                  "(чат-мониторинг продолжит сам).")
            return 1
        question = (
            f"Отпустить #{order_id}: {format_username(row.get('username'))} × {row.get('quantity')}⭐ "
            f"(выручка {row.get('price_rub')} ₽)? Будет куплено ~"
            f"{(row.get('quantity') or 0) / 1000 * cfg.usdt_per_1000_stars:.2f} USDT."
        )
        if not _confirm_or_exit(assume_yes, question):
            return 2
        funpay, gameau, notifier = build_funpay(cfg), build_gameau(cfg), build_notifier(cfg)
        try:
            result = await bot_control.retry_failed_order(
                order_id=order_id,
                db=db,
                funpay_client=funpay,
                gameau_client=gameau,
                tg_notifier=notifier,
                tracker=TaskTracker(db),
                ignore_policy=True,
                **_order_kwargs(cfg),
            )
        finally:
            await funpay.close()
            await gameau.close()
            await notifier.close()
        if as_json:
            print(json.dumps(result, ensure_ascii=False, default=str))
        updated = await db.get_order(order_id)
        state = (updated or {}).get("status")
        ok = state == "COMPLETED"
        detail = (updated or {})
        if ok:
            print(f"✅ Заказ #{order_id} выдан: {state}, затраты {detail.get('cost_usdt')} USDT, "
                  f"прибыль {detail.get('profit_rub')} ₽")
        else:
            print(f"❌ Заказ #{order_id}: результат {result.get('status')}, статус {state} "
                  f"({detail.get('error') or result.get('error') or 'см. --timeline ' + order_id})")
        return 0 if ok else 1


async def cmd_cancel_held(cfg: Config, order_id: str, note: str = "", assume_yes: bool = False) -> int:
    """Отклоняет задержанный заказ (деньги не списывались — просто закрываем сделку)."""
    async with open_db(cfg) as db:
        row = await db.get_order(order_id)
        if row is None:
            print(f"[ERR] Заказ #{order_id} не найден в базе.")
            return 1
        if not _confirm_or_exit(assume_yes, f"Отклонить #{order_id} (статус {row.get('status')}) и вернуть "
                                            f"{row.get('price_rub')} ₽ покупателю на FunPay?"):
            return 2
        tracker = TaskTracker(db)
        await db.save_order_status(
            order_id,
            row.get("username"),
            "CANCELLED",
            error=note or "отклонено вручную (политика выдачи)",
        )
        await tracker.log(order_id, "MANUAL_CANCEL", note or "отклонено оператором")
        print(f"✔ Заказ #{order_id} помечен CANCELLED. Не забудьте отменить сделку на стороне FunPay — "
              "бот её не закрывает и деньги не возвращает.")
        return 0


async def cmd_limits(cfg: Config, as_json: bool = False) -> int:
    """Что политика выдачи считает сегодня и сколько уже потрачено."""
    pol = cfg.order_policy()
    async with open_db(cfg) as db:
        spent = 0.0
        held: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            spent = await db.sum_cost_usdt_since(pol.day_start_ts())
        with contextlib.suppress(Exception):
            held = await TaskTracker(db).held_orders(limit=100)
    payload = {
        "limits": {
            "max_order_revenue_rub": pol.max_order_revenue_rub,
            "min_margin_pct": pol.min_margin_pct,
            "daily_spend_limit_usdt": pol.daily_spend_limit_usdt,
            "duplicate_window_min": pol.duplicate_window_min,
            "duplicate_action": pol.duplicate_action,
            "blacklist_enabled": pol.blacklist_enabled,
        },
        "spent_today_usdt": round(spent, 4),
        "remaining_today_usdt": round(max(0.0, pol.daily_spend_limit_usdt - spent), 4)
        if pol.daily_spend_limit_usdt > 0
        else None,
        "held": len(held),
        "rate_rub_per_usdt": pol.rate_rub_per_usdt,
        "mute_window": cfg.mute_window(),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print("Политика выдачи (защита от убытка):\n")
    lim = payload["limits"]
    rows = [
        ("MAX_ORDER_REVENUE_RUB", f"{lim['max_order_revenue_rub']:.0f} ₽", "держит крупные сделки"),
        ("MIN_MARGIN_PCT", f"{lim['min_margin_pct']:.1f} %", "держит сделки ниже маржи"),
        ("DAILY_SPEND_LIMIT_USDT", f"{lim['daily_spend_limit_usdt']:.2f} USDT", "суточный бюджет на закупку"),
        ("DUPLICATE_WINDOW_MIN", f"{lim['duplicate_window_min']} мин ({lim['duplicate_action']})", "повтор того же ника и количества"),
        ("BLACKLIST_ENABLED", "вкл" if lim["blacklist_enabled"] else "выкл", "стоп-лист покупателей"),
        ("MUTE_HOURS", f"{payload['mute_window'] or '—'}", "тихие часы для некритичных алертов"),
    ]
    off = ("0", "0.0", "0.00 USDT", "выкл", "0 мин (alert)", "0 мин (ignore)", "—")
    for name, value, hint in rows:
        print(f"  {'•' if value not in off else '·'} {name:<24} {value:<22} {hint}")
    if all(v in off for _, v, _ in rows):
        print("\n  Все проверки выключены: бот покупает любой оплаченный заказ. "
              "Начните с MAX_ORDER_REVENUE_RUB и DAILY_SPEND_LIMIT_USDT.")
    print(f"\n  Потрачено с полуночи: {spent:.2f} USDT", end="")
    if pol.daily_spend_limit_usdt > 0:
        print(f" из {pol.daily_spend_limit_usdt:.2f} "
              f"(осталось {payload['remaining_today_usdt']:.2f})")
    else:
        print(" (лимит выключен)")
    print(f"  Задержано заказов: {len(held)}   "
          f"{'— autostars --held' if held else ''}")
    print("\n  Ключи — в .env (или вкладка «Политика» в GUI): 0/пусто = проверка выключена.")
    return 0


async def cmd_blacklist(cfg: Config, action: str, username: str | None = None,
                        note: str = "", as_json: bool = False) -> int:
    """Стоп-лист покупателей: list | add @nick | remove @nick."""
    async with open_db(cfg) as db:
        if action == "add":
            if not username:
                print("[ERR] --blacklist add требует @username.")
                return 1
            await db.add_buyer_flag(username, note=note or None)
            print(f"✅ @{username.lstrip('@')} добавлен(а) в стоп-лист — "
                  "заказы этому покупателю будут задерживаться.")
            return 0
        if action == "remove":
            if not username:
                print("[ERR] --blacklist remove требует @username.")
                return 1
            removed = await db.remove_buyer_flag(username)
            print(f"✅ Убрано записей: {removed}" if removed else f"@{username.lstrip('@')} в стоп-листе не значится.")
            return 0 if removed else 1
        rows = await db.list_buyer_flags("blacklist")
        if as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            return 0
        if not rows:
            print("Стоп-лист пуст. Добавить: autostars --blacklist add @username \"причина\"")
            return 0
        print(f"Стоп-лист покупателей ({len(rows)}):")
        for row in rows:
            print(f"  • @{str(row.get('username')).lstrip('@')}"
                  + (f" — {row.get('note')}" if row.get("note") else ""))
        return 0


async def cmd_customers(cfg: Config, days: float = 7.0, limit: int = 20, as_json: bool = False) -> int:
    """Покупатели за период: оборот, прибыль, провалы — «кого держать на прицеле»."""
    since = int(time.time() - max(days, 1 / 24) * 86400)
    async with open_db(cfg) as db:
        rows = await db.buyer_stats(since, limit=limit)
        if as_json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            return 0
        if not rows:
            print(f"За {days:g} суток заказов не было.")
            return 0
        print(f"Покупатели за {days:g} суток (топ-{len(rows)} по обороту):\n")
        print(f"  {'ник':<20} {'заказов':>8} {'готово':>7} {'провалов':>9} "
              f"{'звёзд':>8} {'оборот ₽':>11} {'прибыль ₽':>10}")
        for row in rows:
            nick = str(row.get("username") or "?")[:19]
            print(f"  {nick:<20} {row['orders']:>8} {row['completed']:>7} {row['failed']:>9} "
                  f"{row['stars']:>8} {row['revenue_rub']:>11.2f} {row['profit_rub']:>10.2f}")
        return 0


async def cmd_export(cfg: Config, out: str, *, fmt: str = "csv", period: str | None = None,
                     status: str | None = None, failed: bool = False, buyer: str | None = None,
                     events: bool = False, limit: int = 0, as_json: bool = False) -> int:
    """Выгружает историю заказов в файл (CSV для Excel / JSON для импорта)."""
    from .services.export import export_csv, export_json, parse_period

    window = parse_period(period)
    since, until = window if window else (None, None)
    async with open_db(cfg) as db:
        writer = export_json if fmt == "json" else export_csv
        try:
            summary = await writer(
                db, out,
                with_events=events,
                since=since,
                until=until,
                status=status,
                only_failed=failed,
                buyer=buyer,
                limit=limit,
            )
        except ValueError as exc:
            print(f"[ERR] {exc}")
            return 1
    summary["period"] = period or "all"
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    print(f"✅ Выгружено {summary['orders']} заказов → {summary['path']}")
    if summary.get("events_path"):
        print(f"   хронология событий ({summary['events']}) → {summary['events_path']}")
    print(f"   выручка {summary['revenue_rub']} ₽, затраты {summary['cost_usdt']} USDT, "
          f"прибыль {summary['profit_rub']} ₽")
    return 0


async def cmd_manual_order(cfg: Config, username: str, quantity: int, *, price_rub: float | None = None,
                           dry_run: bool = False, assume_yes: bool = False) -> int:
    """Ручная выдача: «покупатель просит вручную» — заказ без сделки на FunPay.

    Полезно, когда оплату приняли в обход автовыдачи (спор/перенос/подарок).
    Синтетический ID `M-<timestamp>` нужен, чтобы заказ не столкнулся с реальным
    и чтобы Idempotency-Key GAMEAU был предсказуемым.
    """
    from .services.order_processor import process_paid_order

    order_id = f"M-{int(time.time())}"
    order_data = {
        "id": order_id,
        "chat_node": f"manual-{order_id}",
        "last_message": f"@{username}",
        "description": f"Ручная выдача {quantity}⭐ @{username}",
        "quantity": quantity,
        "price": price_rub if price_rub is not None else 0,
    }
    est_usdt = round(quantity / 1000 * cfg.usdt_per_1000_stars, 4)
    pol = cfg.order_policy()
    est_rub = round(est_usdt * pol.rate_rub_per_usdt, 2)
    print(f"Ручная выдача #{order_id}: {format_username(username)} × {quantity}⭐")
    print(f"  оценка затрат: {est_usdt} USDT ≈ {est_rub:.2f} ₽ "
          f"(курс {pol.rate_rub_per_usdt:.2f} ₽/USDT)")

    if dry_run:
        async with open_db(cfg) as db:
            checks = await evaluate_order_risk(
                pol, db,
                order_id=order_id,
                username=username,
                quantity=quantity,
                price_rub=float(price_rub or 0),
                cost_rub=est_rub,
                order_cost_usdt=est_usdt,
            )
        if checks:
            print("  политика выдачи говорит:")
            for check in checks:
                flag = "⛔" if check.is_hold else "⚠️"
                print(f"    {flag} {check.code} ({check.action}): {check.reason}")
        else:
            print("  политика выдачи: замечаний нет.")
        print("\n✅ --dry-run: запрос в GAMEAU НЕ отправлялся, деньги не списаны.")
        return 0

    if not _confirm_or_exit(assume_yes, f"Купить {quantity}⭐ для {format_username(username)} за ~{est_usdt} USDT?"):
        return 2

    cfg.validate()
    async with open_db(cfg) as db:
        funpay, gameau, notifier = build_funpay(cfg), build_gameau(cfg), build_notifier(cfg)
        try:
            result = await process_paid_order(
                order_data=order_data,
                funpay_client=funpay,
                gameau_client=gameau,
                db=db,
                tg_notifier=notifier,
                task_tracker=TaskTracker(db),
                # политику НЕ обходим: «-y» означает «не спрашивай», а не «игнорируй
                # лимиты». Если что-то заблокировано — заказ уйдёт в HOLD_MANUAL,
                # и дальше решение за --release.
                **_order_kwargs(cfg),
            )
        finally:
            await funpay.close()
            await gameau.close()
            await notifier.close()
    print("Результат: " + json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("status") in ("completed", "unconfirmed") else 1


def _parse_order_args(raw: list[str], cfg: Config, price_override: float | None) -> tuple[str, int, float | None]:
    """`@nick [звёзды]` → (username, quantity, price_rub). Количество по умолчанию — из конфига."""
    if not raw:
        raise ValueError("нужно указать @username покупателя")
    username = str(raw[0]).strip().lstrip("@")
    if not username:
        raise ValueError("пустой @username")
    quantity = int(cfg.default_stars_quantity)
    if len(raw) > 1:
        try:
            quantity = int(float(raw[1]))
        except (TypeError, ValueError):
            raise ValueError(f"не понял количество звёзд: {raw[1]!r}") from None
    if quantity <= 0:
        raise ValueError("количество звёзд должно быть больше нуля")
    return username, quantity, price_override


def cmd_templates(cfg: Config) -> int:
    """Шаблоны ответов покупателю: что сейчас действует и чем можно управлять."""
    from .services.policy import ReplyTemplates

    templates = cfg.reply_templates()
    placeholders = (
        "{username} — ник покупателя без @, "
        "{quantity} — сколько звёзд, {quantity_spaces} — «1 000», "
        "{order_id} — номер заказа, {price_rub} — выручка ₽, {reason} — причина задержки"
    )
    print("Шаблоны ответов в чат FunPay (ENV-ключи в .env / поля в GUI «Ответы покупателю»):\n")
    for kind, env, title in (
        ("need_username", "REPLY_NEED_USERNAME", "запрос ника, если он не распознан"),
        ("delivered", "REPLY_DELIVERED", "звёзды зачислены"),
        ("hold", "REPLY_HOLD", "заказ задержан политикой (пусто = молчим)"),
        ("error", "REPLY_ERROR", "выдача не удалась (пусто = молчим)"),
    ):
        text = getattr(templates, kind) or ""
        state = "(пусто → сообщение не отправляется)" if not text else ""
        print(f"  {env}  {title} {state}")
        for line in text.splitlines() or [""]:
            print(f"      {line}" if line else "")
        print()
    print(f"Плейсхолдеры: {placeholders}")
    print("Неизвестный плейсхолдер остаётся в тексте как есть — ответ не рассыпается.")
    print(f"Значения по умолчанию: {ReplyTemplates().delivered.splitlines()[0][:56]}…")
    return 0


async def cmd_metrics(cfg: Config) -> int:
    """Тот же снимок, что отдаёт /metrics, — для отладки и cron-проверок."""
    from .metrics import render_prometheus

    pol = cfg.order_policy()
    snapshot: dict[str, Any] = {"running": True, "config_ok": not any(i.startswith("missing:") for i in cfg.issues())}
    async with open_db(cfg) as db:
        day_start = pol.day_start_ts()
        with contextlib.suppress(Exception):
            window = await db.window_stats(start_ts=day_start)
            counts = await db.get_status_counts()
            snapshot.update(
                {
                    "status_counts": counts,
                    "today": {
                        "total": window.get("total", 0),
                        "revenue_rub": round(float(window.get("revenue_rub") or 0.0), 2),
                        "cost_usdt": round(float(window.get("cost_usdt") or 0.0), 4),
                        "profit_rub": round(float(window.get("profit_rub") or 0.0), 2),
                    },
                    "limits": {
                        "spent_usdt": round(await db.sum_cost_usdt_since(day_start), 4),
                        "limit_usdt": float(cfg.daily_spend_limit_usdt or 0),
                        "held": int(counts.get("HOLD_MANUAL", 0)),
                    },
                    "paused": await bot_control.is_bot_paused(db),
                }
            )
    print(render_prometheus(snapshot), end="")
    return 0


async def cmd_stats_push(cfg: Config) -> int:
    """Отчёт по окнам в Telegram (или в консоль, если Telegram не настроен)."""
    async with open_db(cfg) as db:
        stats = build_statistics(db, cfg)
        text = stats.render_telegram(await stats.compute_all())
        notifier = build_notifier(cfg)
        try:
            if not notifier.is_configured:
                print("[WARN] Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID). Отчёт:")
                print(text)
                return 1
            ok = await notifier.send_stats_report(text)
            print("✅ Отчёт отправлен в Telegram." if ok else "❌ Не удалось отправить отчёт в Telegram.")
            return 0 if ok else 1
        finally:
            await notifier.close()


def cmd_config_show(cfg: Config) -> int:
    """Активная конфигурация с замаскированными секретами — «что реально применилось»."""
    data = cfg.to_safe_dict()
    order = ("golden_key", "gameau_key", "funpay_base_url", "gameau_base_url", "telegram_bot_token", "telegram_chat_id")
    keys = [k for k in order if k in data] + sorted(k for k in data if k not in order)
    width = max(len(k) for k in keys)
    print("\n⚙️  АКТИВНАЯ КОНФИГУРАЦИЯ (секреты замаскированы)")
    print("=" * (width + 24))
    for key in keys:
        value = data[key]
        if isinstance(value, float):
            value = f"{value:g}"
        print(f"  {key:<{width}} = {value}")
    print("=" * (width + 24))
    if cfg.source_files:
        print("  источники: " + ", ".join(cfg.source_files))
    if cfg.parse_errors:
        print("\n  ⚠️ проблемы разбора:")
        for message in cfg.parse_errors:
            print(f"     • {message}")
    issues = cfg.issues()
    if issues:
        print("\n  ⚠️ замечания:")
        for message in issues:
            print(f"     • {message}")
    print()
    return 0


# ============================================================================ #
# Диагностика и каталог
# ============================================================================ #

async def check_system(cfg: Config, printer: Printer = print) -> bool:
    """Диагностика конфигурации, БД, FunPay, GAMEAU и Telegram. True — всё готово."""
    emit = lambda text="": printer(text)  # noqa: E731
    emit("\n🔍 === ДИАГНОСТИКА СИСТЕМЫ AUTOSTARS ===")
    all_ok = True

    def check(label: str, ok: bool, detail: str = "", warn_only: bool = False) -> None:
        nonlocal all_ok
        mark = "[OK]  " if ok else ("[WARN]" if warn_only else "[ERR] ")
        emit(f"{mark} {label}" + (f": {detail}" if detail else ""))
        if not ok and not warn_only:
            all_ok = False

    # 0. Конфигурация
    issues = cfg.issues()
    fatal = [i for i in issues if i.startswith("missing:")]
    check(
        "Конфигурация",
        not fatal,
        (f"файлы: {', '.join(Path(p).name for p in cfg.source_files) or 'только переменные окружения / значения по умолчанию'}")
        if not fatal
        else ", ".join(fatal),
    )
    for issue in issues:
        if not issue.startswith("missing:"):
            emit(f"       ⚠️  {issue}")

    # 1. База данных
    try:
        async with open_db(cfg) as db:
            counts = await db.get_status_counts()
        check("SQLite", True, f"{cfg.db_path} (заказов в базе: {sum(counts.values()) if counts else 0})")
    except Exception as exc:
        check("SQLite", False, str(exc))

    # 2. FunPay
    funpay = build_funpay(cfg)
    try:
        await funpay.refresh_csrf(force=True)
        check("FunPay", True, f"продавец {funpay.username or '?'}, user_id={funpay.user_id}, csrf {mask_secret(cfg.golden_key)}")
    except FunPayError as exc:
        check("FunPay", False, str(exc))
    except Exception as exc:
        check("FunPay", False, f"{type(exc).__name__}: {exc}")
    finally:
        await funpay.close()

    # 3. GAMEAU
    gameau = build_gameau(cfg)
    try:
        ping = await gameau.ping()
        if ping.get("ok"):
            account = ping.get("account") or {}
            balance = ping.get("balance")
            detail = f"баланс {balance:.2f} {account.get('currency', 'USDT')}" if balance is not None else "ключ принят"
            check("GAMEAU", True, detail)
            if balance is not None and balance < cfg.low_balance_threshold_usdt:
                emit(
                    f"       ⚠️  баланс ниже порога {cfg.low_balance_threshold_usdt:.2f} — "
                    "выдача встанет с LOW_BALANCE; пополните USDT TRC-20 (--deposit-info)"
                )
        else:
            check("GAMEAU", False, f"{ping.get('error')} {ping.get('detail', '')}".strip())
    except Exception as exc:
        check("GAMEAU", False, f"{type(exc).__name__}: {exc}")
    finally:
        await gameau.close()

    # 4. Каталог и себестоимость
    gameau = build_gameau(cfg)
    try:
        package = await gameau.find_stars_package(cfg.default_stars_quantity)
        if package:
            price = GameauClient.item_price_usdt(package) or 0.0
            stars = GameauClient.extract_item_stars(package) or cfg.default_stars_quantity
            emit(
                f"[OK]  Каталог: пакет {stars}⭐ за {price:.2f} USDT "
                f"(≈ {price * cfg.active_usdt_rate:.2f} ₽ по активному курсу)"
            )
        else:
            emit(
                f"[WARN] Каталог недоступен — себестоимость будет оценочная "
                f"({cfg.usdt_per_1000_stars:.2f} USDT / 1000⭐)"
            )
    except Exception as exc:
        emit(f"[WARN] Каталог недоступен: {exc}")
    finally:
        await gameau.close()

    # 5. Telegram
    notifier = build_notifier(cfg)
    try:
        if notifier.is_configured:
            sent = await notifier.send_alert("✅ <b>AutoStars — диагностика</b>: это тестовое сообщение из --check")
            check("Telegram", sent, f"chat_id={cfg.telegram_chat_id}" + ("" if sent else " — сообщение не доставлено"), warn_only=not sent)
        else:
            emit("[INFO] Telegram не настроен — уведомления и TG-команды выключены (опционально)")
    except Exception as exc:
        check("Telegram", False, str(exc), warn_only=True)
    finally:
        await notifier.close()

    # 6. Финансовая модель
    emit("\n📊 Финансовая модель завода USDT TRC-20:")
    emit(f"   • Вариант 1 (анонимный обмен/P2P): {cfg.rate_variant_1:.2f} ₽/USDT")
    emit(f"   • Вариант 2 (Whitebird, РБ):       {cfg.rate_variant_2:.2f} ₽/USDT")
    emit(
        f"   • активный: вариант {cfg.exchange_variant} → {cfg.active_usdt_rate:.2f} ₽/USDT; "
        f"1000⭐ ≈ {cfg.estimate_cost_usdt(1000) * cfg.active_usdt_rate:.2f} ₽"
    )
    emit(
        f"🛡 Лимит maxCharge: ≥ {cfg.default_max_charge_usdt:.2f} USDT "
        f"(запас к цене пакета +{cfg.max_charge_margin_pct:g}%, concurrency {cfg.max_concurrent_orders})"
    )
    emit(
        f"🗂 Трекинг: зависание > {cfg.stuck_task_minutes} мин, reconcile каждые "
        f"{cfg.reconciliation_interval_sec}s, повторов {cfg.max_order_retries}"
    )
    emit(
        f"⏱ Цикл: poll {cfg.poll_interval:g}s, чат-мониторинг {cfg.chat_monitor_interval_sec}s, "
        f"баланс каждые {cfg.balance_check_interval_min} мин, "
        f"статистика в TG каждые {cfg.stats_push_interval_min} мин"
    )
    emit(f"📝 Лог: {cfg.log_file} (ротация {cfg.log_max_bytes / 1_000_000:.1f} МБ × {cfg.log_backup_count})")
    emit("=" * 56)
    emit("✅ всё готово к запуску" if all_ok else "❌ есть проблемы — см. строки [ERR]")
    emit()
    return all_ok


async def show_catalog(cfg: Config, item_type: str = "telegramStars", printer: Printer = print) -> None:
    """Каталог GAMEAU с себестоимостью по обоим вариантам курса."""
    gameau = build_gameau(cfg)
    emit = lambda text="": printer(text)  # noqa: E731
    emit(f"\n📦 === КАТАЛОГ GAMEAU ({item_type}) ===")
    try:
        items = await gameau.get_catalog(item_type=item_type)
        if not items:
            emit("Каталог пуст или недоступен (проверьте GAMEAU_API_KEY / сеть).")
            return

        emit(f"{'Пакет':<34} | {'⭐':>6} | {'USDT':>7} | {'Вариант 2, ₽':>13} | {'Вариант 1, ₽':>13}")
        emit("-" * 88)
        for item in items:
            name = str(item.get("name") or item.get("title") or "—")[:34]
            price = GameauClient.item_price_usdt(item) or 0.0
            stars = GameauClient.extract_item_stars(item) or 0
            emit(
                f"{name:<34} | {stars:>6} | {price:>7.2f} | "
                f"{price * cfg.rate_variant_2:>13.2f} | {price * cfg.rate_variant_1:>13.2f}"
            )
        emit("=" * 88)
        emit("💡 Это цены вашего тарифа GAMEAU; себестоимость = цена пакета × курс завода USDT.")
    except Exception as exc:
        emit(f"[ERR] Ошибка загрузки каталога: {exc}")
    finally:
        await gameau.close()


# ============================================================================ #
# Финансовый калькулятор и завод крипты
# ============================================================================ #

def estimate_usdt_cost(cfg: Config, stars: int, catalog_price_usdt: float | None = None) -> float:
    """Себестоимость заказа: цена пакета из каталога, иначе — оценка по курсу."""
    if catalog_price_usdt:
        return round(float(catalog_price_usdt), 4)
    return cfg.estimate_cost_usdt(stars)


def profit_calc_text(cfg: Config, price_rub: float, stars: int = 1000, usdt_cost: float | None = None) -> str:
    """Текст калькулятора прибыли по обоим вариантам завода USDT."""
    cost = usdt_cost or cfg.estimate_cost_usdt(stars)
    lines = [
        f"\n📊 === КАЛЬКУЛЯТОР ПРИБЫЛИ ДЛЯ {stars} STARS ===",
        f"Выручка на FunPay: {price_rub:.2f} ₽ | Затраты в GAMEAU: {cost:.2f} USDT",
        "-" * 65,
    ]
    profits: dict[int, float] = {}
    for variant, rate in ((1, cfg.rate_variant_1), (2, cfg.rate_variant_2)):
        cost_rub = cost * rate
        profit = price_rub - cost_rub
        profits[variant] = profit
        margin = profit / price_rub * 100 if price_rub else 0.0
        label = "анонимный обмен/P2P" if variant == 1 else "Whitebird (РБ)"
        mark = " 💰 рекомендуется" if variant == cfg.exchange_variant else ""
        lines.append(
            f"• Вариант {variant} ({label}, {rate:.2f} ₽/USDT){mark}\n"
            f"    себестоимость {cost_rub:.2f} ₽ | прибыль {profit:+.2f} ₽ | маржа {margin:.1f}%"
        )
    diff = profits[2] - profits[1]
    lines += [
        "-" * 65,
        f"🚀 Whitebird экономит {diff:.2f} ₽ на этом заказе "
        f"({diff * 30:.0f} ₽/мес при 30 таких сделках)",
        "=" * 65,
    ]
    return "\n".join(lines) + "\n"


def print_profit_calc(cfg: Config, price_rub: float, stars: int = 1000, usdt_cost: float | None = None) -> None:
    print(profit_calc_text(cfg, price_rub, stars, usdt_cost))


def deposit_info(cfg: Config) -> str:
    """Инструкция по заводу USDT TRC-20 на баланс GAMEAU (оба варианта)."""
    cost = cfg.usdt_per_1000_stars
    return f"""
💎 === ЗАВОД USDT (TRC-20) НА BALANCE GAMEAU.US ===

Бот списывает USDT с баланса GAMEAU за каждую выдачу. Как его пополнить:

1️⃣ ВАРИАНТ 1 — анонимный обмен / P2P (курс ≈ {cfg.rate_variant_1:.2f} ₽/USDT)
   • Плюсы:  без верификации.
   • Минусы: дороже на {cfg.rate_variant_1 - cfg.rate_variant_2:.2f} ₽/USDT, риски блокировок карт (115-ФЗ).
   • Способы: Telegram Wallet P2P, BestChange (карта → Tether TRC20), криптоматы.
   • 1000⭐ ({cost:.2f} USDT) ≈ {cost * cfg.rate_variant_1:.2f} ₽.

2️⃣ ВАРИАНТ 2 — Whitebird, Беларусь, без P2P (курс ≈ {cfg.rate_variant_2:.2f} ₽/USDT) [рекомендуется]
   • Плюсы:  официальный обмен (без блокировок), дешевле на {cost * (cfg.rate_variant_1 - cfg.rate_variant_2):.2f} ₽ на 1000⭐.
   • Минусы: разовая верификация.
   • Порядок: whitebird.io → покупка USDT (TRC-20) на карту → адрес получателя =
     депозитный адрес из кабинета gameau.us (Профиль → Пополнить → USDT TRC-20).
   • 1000⭐ ≈ {cost * cfg.rate_variant_2:.2f} ₽.

⚡ КОМИССИЯ СЕТИ TRON
   • При частых переводах выгодно арендовать Tron Energy (Feeee.io / JustLend):
     транзакция стоит ~6-8 TRX вместо ~28 TRX.
   • Комиссию завода можно учесть в отчётности: TRON_ENERGY_FEE_RUB (₽ на сделку).

🧮 Проверка: python -m autostars.main --calc 1370.40 1000
   Каталог GAMEAU:   python -m autostars.main --catalog
"""


def print_deposit_info(cfg: Config) -> None:
    print(deposit_info(cfg))


# ============================================================================ #
# Тестовый заказ (тратит реальные деньги!)
# ============================================================================ #

async def run_test_order(
    cfg: Config,
    username: str,
    quantity: int = 50,
    *,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> int:
    """Прогон пайплайна на одном заказе. `--dry-run` — без списания USDT."""
    gameau = build_gameau(cfg)
    try:
        package = None
        with contextlib.suppress(Exception):
            package = await gameau.find_stars_package(quantity)
        price = GameauClient.item_price_usdt(package) if package else None
        stars_in_package = GameauClient.extract_item_stars(package) if package else None
        cost = estimate_usdt_cost(cfg, quantity, price)
        charge = cfg.compute_max_charge(quantity, price)
    finally:
        await gameau.close()

    print("\n🧪 ТЕСТОВЫЙ ЗАКАЗ")
    print(f"   получатель: @{username}")
    print(f"   звёзды:     {quantity}" + (f" (пакет в каталоге: {stars_in_package}⭐)" if stars_in_package else " (пакет в каталоге не найден)"))
    print(f"   себестоимость: {cost:.2f} USDT ≈ {cost * cfg.active_usdt_rate:.2f} ₽ "
          f"({cfg.active_rate_label})")
    print(f"   maxCharge:  {charge:.2f} USDT")
    print(f"   hide_sender: {'вкл' if cfg.hide_sender else 'выкл'}")

    if dry_run:
        print("\n✅ --dry-run: запрос в GAMEAU НЕ отправлялся. Без него заказ будет реально куплен.")
        return 0

    if not assume_yes:
        if not sys.stdin.isatty():
            print("\n❌ Тестовый заказ покупает звёзды за реальные USDT. "
                  "Подтвердите запуск флагом --yes (или сначала сделайте --dry-run).")
            return 2
        answer = input("\nОтправить реальный заказ? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "да"):
            print("Отменено.")
            return 1

    cfg.validate()
    async with open_db(cfg) as db:
        funpay, notifier = build_funpay(cfg), build_notifier(cfg)
        gameau = build_gameau(cfg)
        test_order = {
            "id": f"TEST_{int(time.time())}",
            "chat_node": "test_chat",
            "last_message": f"@{username}",
            "description": f"Тестовый заказ {quantity} Stars",
            "quantity": quantity,
            "price": round(cost * cfg.active_usdt_rate, 2),
        }
        try:
            result = await process_paid_order(
                order_data=test_order,
                funpay_client=funpay,
                gameau_client=gameau,
                db=db,
                tg_notifier=notifier,
                task_tracker=TaskTracker(db),
                **_order_kwargs(cfg),
            )
            print(f"\nРезультат: {json.dumps(result, ensure_ascii=False, default=str)}")
            return 0 if result.get("status") == "completed" else 1
        finally:
            await funpay.close()
            await gameau.close()
            await notifier.close()


# ============================================================================ #
# Основной цикл автовыдачи
# ============================================================================ #

def install_stop_handlers(stop_event: asyncio.Event) -> None:
    """SIGTERM/SIGINT/SIGBREAK → stop_event (graceful shutdown вместо SIGKILL)."""
    loop = asyncio.get_running_loop()

    def _via_signal(_signum: int = 0, _frame: Any = None) -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(stop_event.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError, ValueError, OSError):
            # Windows: add_signal_handler не поддерживается
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, _via_signal)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    """Сон, прерываемый остановкой — чтобы `stop` не ждал полного интервала."""
    if seconds <= 0:
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)


async def _login_with_retry(
    funpay: FunPayClient,
    cfg: Config,
    stop_event: asyncio.Event,
    notifier: TelegramNotifier | None = None,
) -> bool:
    """Логин FunPay с backoff: кратковременныйFunPay/DFI не должен ронять сервис."""
    attempt = 0
    alerted = False
    while not stop_event.is_set():
        attempt += 1
        try:
            await funpay.refresh_csrf(force=True)
            if attempt > 1:
                logger.info(f"Сессия FunPay восстановлена после {attempt - 1} попыток")
            return True
        except FunPayError as exc:
            delay = min(cfg.startup_retry_delay * (2 ** min(attempt - 1, 4)), 300.0)
            logger.error(f"FunPay недоступен (попытка {attempt}): {exc}; повтор через {delay:.0f}с")
            if attempt >= 3 and notifier is not None and notifier.is_configured and not alerted:
                alerted = True
                with contextlib.suppress(Exception):
                    await notifier.send_alert(
                        f"🚨 <b>FunPay недоступен</b>\n{exc}\n"
                        f"AutoStars не может начать работу и продолжает попытки "
                        f"(каждые ~{delay:.0f}с). Проверьте FUNPAY_GOLDEN_KEY."
                    )
            if cfg.startup_max_retries and attempt >= cfg.startup_max_retries:
                logger.error(f"Логин FunPay не удался за {attempt} попыток — выходим")
                return False
            await _sleep_or_stop(stop_event, delay)
    return False


async def run_bot(cfg: Config, once: bool = False, stop_event: asyncio.Event | None = None) -> None:
    """Главный цикл: опрос оплаченных заказов, чат-мониторинг, reconciliation."""
    cfg.validate()
    stop_event = stop_event or asyncio.Event()
    install_stop_handlers(stop_event)

    async def _request_stop() -> None:
        """Остановка по сигналу / TG-команде /stop."""
        stop_event.set()

    async with open_db(cfg) as db:
        funpay, gameau, notifier = build_funpay(cfg), build_gameau(cfg), build_notifier(cfg)
        tracker = TaskTracker(db)
        stats = build_statistics(db, cfg)
        tasks: set[asyncio.Task] = set()
        sem = asyncio.Semaphore(max(1, cfg.max_concurrent_orders))

        logger.info("Инициализация сессии FunPay...")
        if not await _login_with_retry(funpay, cfg, stop_event, notifier):
            await funpay.close()
            await gameau.close()
            await notifier.close()
            return
        logger.info(f"Сессия FunPay активна (продавец: {funpay.username or '?'}); {cfg.summary()}")

        if await bot_control.is_bot_paused(db):
            logger.warning(
                "Бот запущен в режиме ПАУЗЫ (новые заказы не принимаются). "
                "Снять: python -m autostars.main --resume или /resume в Telegram."
            )

        if notifier.is_configured:
            with contextlib.suppress(Exception):
                await notifier.send_alert(
                    "🤖 <b>AUTOSTARS запущен</b>\n"
                    f"Курс USDT: {cfg.active_rate_label} = {cfg.active_usdt_rate:.2f} ₽\n"
                    f"maxCharge ≥ {cfg.default_max_charge_usdt:.2f} USDT (+{cfg.max_charge_margin_pct:g}%) | "
                    f"poll {cfg.poll_interval:g}s | reconcile {cfg.reconciliation_interval_sec}s | "
                    f"чат-мониторинг {cfg.chat_monitor_interval_sec}s"
                )

        balance = None
        with contextlib.suppress(Exception):
            balance = await bot_control.check_gameau_balance(gameau, notifier, cfg.low_balance_threshold_usdt, alert=True)
        if balance:
            logger.info(f"Баланс GAMEAU при старте: {balance['balance']:.2f} {balance['currency']}")

        tg_server: TelegramCommandServer | None = None
        tg_task: asyncio.Task | None = None
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
                        "stop": _request_stop,
                    },
                ),
            )
            tg_task = asyncio.create_task(tg_server.run(stop_event), name="tg-commands")
            logger.info("Интерактивный TG-бот: запущен (/help — список команд)")

        # Метрики/healthz — по умолчанию выключены (METRICS_ENABLED=false):
        # наружу не торчит ничего, а включив, получаем готовый scrape для Prometheus
        # и /healthz для uptime-мониторинга. Снимок обновляется раз в 15 с, scrape
        # только читает кэш — база под нагрузкой мониторинга не дрогнет.
        metrics_cache: dict[str, Any] = {"running": True, "config_ok": True, "status_counts": {}}
        metrics_server: Any = None
        if cfg.metrics_enabled:
            from .metrics import MetricsServer

            try:
                metrics_server = MetricsServer(
                    lambda: metrics_cache,
                    host=cfg.metrics_host,
                    port=cfg.metrics_port,
                    token=cfg.metrics_token,
                )
                await metrics_server.serve()
            except ValueError as exc:
                # наружу без токена — не поднимаем вовсе (конфиг это же ругает в --check)
                logger.error(f"Метрики выключены: {exc}")
                metrics_server = None
            except OSError as exc:
                logger.warning(f"Метрики не подняты ({cfg.metrics_host}:{cfg.metrics_port}): {exc}")
                metrics_server = None

        async def refresh_metrics_snapshot() -> None:
            """Считает снимок для /metrics и /status (ошибки не должны влиять на цикл)."""
            try:
                pol = cfg.order_policy()
                day_start = pol.day_start_ts()
                window = await db.window_stats(start_ts=day_start)
                counts = await db.get_status_counts()
                issues = cfg.issues()
                metrics_cache.update(
                    {
                        "running": True,
                        "ts": int(time.time()),
                        "paused": await bot_control.is_bot_paused(db),
                        "config_ok": not any(i.startswith("missing:") for i in issues),
                        "issues": issues,
                        "status_counts": counts,
                        "today": {
                            "total": window.get("total", 0),
                            "revenue_rub": round(float(window.get("revenue_rub") or 0.0), 2),
                            "cost_usdt": round(float(window.get("cost_usdt") or 0.0), 4),
                            "profit_rub": round(float(window.get("profit_rub") or 0.0), 2),
                        },
                        "limits": {
                            # считать по sum_cost_usdt_since: суточный лимит
                            # сравнивается именно со списаниями, а не с себестоимостью
                            # завершённых заказов (окно window_stats их не включает)
                            "spent_usdt": round(await db.sum_cost_usdt_since(day_start), 4),
                            "limit_usdt": float(cfg.daily_spend_limit_usdt or 0),
                            "held": int(counts.get("HOLD_MANUAL", 0)),
                        },
                    }
                )
            except Exception as exc:
                logger.debug(f"Снимок метрик не обновлён: {exc}")

        if metrics_server is not None:
            await refresh_metrics_snapshot()

        last = {"reconcile": 0.0, "stats_push": 0.0, "chat_monitor": 0.0, "balance": time.time(),
                "metrics": 0.0}
        stats_push_sec = cfg.stats_push_interval_min * 60.0
        balance_check_sec = cfg.balance_check_interval_min * 60.0

        async def handle_order(order: dict) -> None:
            async with sem:
                try:
                    await process_paid_order(
                        order_data=order,
                        funpay_client=funpay,
                        gameau_client=gameau,
                        db=db,
                        tg_notifier=notifier,
                        task_tracker=tracker,
                        **_order_kwargs(cfg),
                    )
                except Exception as exc:
                    logger.error(f"[ORDER {order.get('id')}] Необработанная ошибка пайплайна: {exc}", exc_info=True)

        try:
            while not stop_event.is_set():
                try:
                    paused = await bot_control.is_bot_paused(db)

                    if not paused:
                        paid_orders = await funpay.get_paid_orders()
                        if paid_orders:
                            logger.info(f"Найдено оплаченных заказов: {len(paid_orders)}")
                            for order in paid_orders:
                                order_id = str(order.get("id"))
                                if not order_id or await db.is_order_processed(order_id):
                                    continue
                                task = asyncio.create_task(handle_order(order), name=f"order-{order_id}")
                                tasks.add(task)
                                task.add_done_callback(tasks.discard)

                        now = time.time()
                        if now - last["chat_monitor"] >= cfg.chat_monitor_interval_sec:
                            last["chat_monitor"] = now
                            try:
                                await bot_control.process_waiting_username_orders(
                                    db=db,
                                    funpay_client=funpay,
                                    gameau_client=gameau,
                                    tg_notifier=notifier,
                                    tracker=tracker,
                                    **_order_kwargs(cfg),
                                )
                            except Exception as exc:
                                logger.error(f"Ошибка чат-мониторинга: {exc}", exc_info=True)
                    else:
                        logger.debug("Бот в паузе — приём новых заказов остановлен")

                    if once:
                        if tasks:
                            await asyncio.gather(*tasks, return_exceptions=True)
                        logger.info("Однократный проход завершён")
                        break

                    # long polling: сигнал о новых событиях (таймаут у него свой)
                    if not paused:
                        with contextlib.suppress(Exception):
                            await funpay.poll_runner()

                    now = time.time()
                    if now - last["reconcile"] >= cfg.reconciliation_interval_sec:
                        last["reconcile"] = now
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

                    if stats_push_sec > 0 and now - last["stats_push"] >= stats_push_sec:
                        last["stats_push"] = now
                        try:
                            await _push_periodic_stats(db, notifier, stats)
                        except Exception as exc:
                            logger.error(f"Ошибка отправки периодической статистики: {exc}", exc_info=True)

                    if balance_check_sec > 0 and now - last["balance"] >= balance_check_sec:
                        last["balance"] = now
                        try:
                            balance = await bot_control.check_gameau_balance(
                                gameau, notifier, cfg.low_balance_threshold_usdt, alert=True
                            )
                        except Exception as exc:
                            logger.error(f"Ошибка проверки баланса GAMEAU: {exc}", exc_info=True)

                    if metrics_server is not None and now - last["metrics"] >= 15.0:
                        last["metrics"] = now
                        await refresh_metrics_snapshot()
                        if balance:
                            metrics_cache["gameau"] = {
                                "balance_usdt": balance.get("balance"),
                                "low_balance": bool(balance.get("low_balance")),
                            }

                except Exception as exc:
                    logger.error(f"Ошибка в цикле автовыдачи: {exc}", exc_info=True)

                await _sleep_or_stop(stop_event, cfg.poll_interval)
        finally:
            if stop_event.is_set():
                logger.info(f"Остановка: дожидается до {len(tasks)} задач выдачи (до {cfg.shutdown_timeout:.0f}с)...")
            if tasks:
                pending = list(tasks)
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True), timeout=cfg.shutdown_timeout
                    )
                for task in pending:
                    if not task.done():
                        task.cancel()
            stop_event.set()
            metrics_cache["running"] = False
            if metrics_server is not None:
                with contextlib.suppress(Exception):
                    await metrics_server.stop()
            if tg_task is not None:
                tg_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await tg_task
            for closer in (funpay.close, gameau.close, notifier.close):
                with contextlib.suppress(Exception):
                    await closer()
            if notifier.is_configured:
                with contextlib.suppress(Exception):
                    await notifier.send_alert("🛑 <b>AUTOSTARS остановлен</b>")
            logger.info("AutoStars остановлен корректно: состояние задач — в БД и журнале task_events")


async def _push_periodic_stats(db: DBManager, notifier: TelegramNotifier, stats: StatisticsService) -> None:
    """Периодическая сводка (1ч / сегодня / 24ч / всего) в Telegram владельца."""
    windows = [
        await stats.window("1 час", 1),
        await stats.day_window(),
        await stats.today_window(),
        await stats.total_window(),
    ]
    if await notifier.send_stats_report(stats.render_telegram(windows)):
        logger.info("Периодическая статистика отправлена в Telegram")


# ============================================================================ #
# CLI
# ============================================================================ #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autostars",
        description="AutoStars: асинхронная автовыдача Telegram Stars на FunPay через GAMEAU B2B API.",
        epilog="Перед запуском: python check_env.py и python -m autostars.main --check",
    )
    parser.add_argument("--once", action="store_true", help="однократный проход по очереди (для cron)")
    parser.add_argument("--check", action="store_true", help="диагностика конфигурации, FunPay, GAMEAU, БД, Telegram")
    parser.add_argument("--catalog", action="store_true", help="каталог GAMEAU с себестоимостью по обоим курсам")
    parser.add_argument("--deposit-info", action="store_true", help="инструкция по заводу USDT TRC-20 (Вариант 1 vs 2)")
    parser.add_argument("--calc", nargs="+", metavar="ARG", help="калькулятор прибыли: --calc 1370.40 [1000] [USDT]")
    parser.add_argument("--stats", action="store_true", help="статистика 1/2/3/4/6/24ч, календарный день, всего")
    parser.add_argument("--stats-hours", default=None, help="свои часовые окна для --stats: --stats-hours 1,6,24")
    parser.add_argument("--report", action="store_true", help="полный отчёт: P&L, убытки, топ сделок, провалы, трекинг")
    parser.add_argument("--tasks", action="store_true", help="трекинг задач: открытые/зависшие/журнал")
    parser.add_argument("--timeline", metavar="ORDER_ID", help="жизненный цикл задачи по ID заказа FunPay")
    parser.add_argument("--stats-push", action="store_true", help="отправить текущую статистику в Telegram")
    parser.add_argument("--balance", action="store_true", help="баланс GAMEAU и прогноз по заказам")
    parser.add_argument("--pause", action="store_true", help="остановить приём новых заказов (переживает рестарт)")
    parser.add_argument("--resume", action="store_true", help="возобновить приём новых заказов")
    parser.add_argument("--retry-order", metavar="ORDER_ID", help="повторить проваленный заказ (идемпотентно)")
    parser.add_argument("--config-show", action="store_true", help="показать применённую конфигурацию (секреты скрыты)")
    parser.add_argument("--json", action="store_true", help="машинный вывод для --stats/--report/--tasks/--balance")
    parser.add_argument("--test-order", metavar="USERNAME", help="тестовый заказ на указанный @username")
    parser.add_argument("--test-qty", type=int, default=50, help="звёзд в тестовом заказе (по умолчанию 50)")
    parser.add_argument("--dry-run", action="store_true", help="для --test-order: без покупки в GAMEAU")
    parser.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение для --test-order")
    parser.add_argument("--held", action="store_true",
                        help="заказы, задержанные политикой выдачи (ждут решения)")
    parser.add_argument("--release", metavar="ORDER_ID", default=None,
                        help="отпустить задержанный заказ и выдать (нужно -y)")
    parser.add_argument("--cancel", metavar="ORDER_ID", default=None,
                        help="отклонить задержанный заказ (нужно -y)")
    parser.add_argument("--note", default="", metavar="TEXT", help="комментарий к --cancel/--blacklist add")
    parser.add_argument("--order", nargs="+", metavar="ARG",
                        help="ручная выдача: --order @username [ЗВЁЗДЫ] (без -y не покупает)")
    parser.add_argument("--order-price", type=float, default=None, metavar="RUB",
                        help="выручка ₽ для --order (для отчёта и проверки маржи)")
    parser.add_argument("--limits", action="store_true",
                        help="политика выдачи: лимиты, суточный бюджет, задержки")
    parser.add_argument("--blacklist", nargs="*", metavar="ARG",
                        help="стоп-лист: --blacklist list | add @nick [причина] | remove @nick")
    parser.add_argument("--customers", action="store_true", help="сводка по покупателям за период")
    parser.add_argument("--days", type=float, default=7.0, help="период для --customers (суток, по умолчанию 7)")
    parser.add_argument("--export", metavar="PATH", default=None,
                        help="выгрузить историю заказов в файл (.csv или .json)")
    parser.add_argument("--since", default=None, metavar="PERIOD",
                        help="период выгрузки: 7d, 24h, 2026-09-01..2026-09-07")
    parser.add_argument("--export-status", default=None, metavar="STATUSES",
                        help="фильтр по статусам через запятую (COMPLETED,FAILED)")
    parser.add_argument("--failed", action="store_true", help="в выгрузку — только проблемные заказы")
    parser.add_argument("--buyer", default=None, metavar="USERNAME", help="в выгрузку — заказы одного покупателя")
    parser.add_argument("--with-events", action="store_true",
                        help="выгрузить и хронологию событий вторым файлом")
    parser.add_argument("--export-limit", type=int, default=0, help="максимум строк в выгрузке (0 = все)")
    parser.add_argument("--metrics", action="store_true",
                        help="напечатать снимок метрик в формате Prometheus")
    parser.add_argument("--templates", action="store_true",
                        help="текущие шаблоны ответов покупателю и доступные плейсхолдеры")
    parser.add_argument("--config", default=None, metavar="PATH", help="путь к config.json (по умолчанию рядом с exe/проектом)")
    parser.add_argument("-v", "--verbose", action="store_true", help="отладочный уровень логов")
    parser.add_argument("--version", action="version", version=f"AutoStars {__version__}")
    return parser


def _parse_calc_args(raw: list[str]) -> tuple[float, int, float | None]:
    """--calc 1370.40 [1000] [9.10] → (цена ₽, звёзды, USDT-себестоимость или None)."""
    price = float(raw[0])
    stars = int(raw[1]) if len(raw) > 1 else 1000
    usdt = float(raw[2]) if len(raw) > 2 else None
    if price <= 0 or stars <= 0:
        raise ValueError("цена и количество звёзд должны быть больше нуля")
    return price, stars, usdt


def main(argv: list[str] | None = None) -> None:
    """Точка входа CLI (без SystemExit — удобно вызывать из GUI и тестов)."""
    # На Windows: переключаем stdout/stderr на UTF-8 чтобы emoji и кириллица не падали
    if os.name == "nt":
        import io
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        else:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )

    args = build_parser().parse_args(argv)
    # сообщение о создании печатаем сами (ниже) — с пояснением про локальные команды
    created = ensure_env_file(quiet=True)
    cfg = Config.load(args.config)
    setup_logging(verbose=args.verbose, cfg=cfg)
    for message in cfg.parse_errors:
        logger.warning(f"Конфигурация: {message}")
    if created:
        # Первый запуск: файл создан из .env.example, но ключей ещё нет.
        # Молча выйти с кодом 0 здесь нельзя — systemd/`echo $?`/GUI решили бы,
        # что всё в порядке, а бот на самом деле ничего не сделал.
        print(
            f"⚠️  Создан файл конфигурации {created} — заполните FUNPAY_GOLDEN_KEY "
            "и GAMEAU_API_KEY, затем запустите команду снова."
        )
        print("   Локальные команды (--check, --calc, --config-show) работают уже сейчас.")
        local_only = (
            args.check or args.calc or args.config_show or args.templates or args.limits
            or args.held or args.customers or args.export or args.metrics
            or (args.blacklist and (len(args.blacklist) == 1 and args.blacklist[0] == "list"))
        )
        if not local_only:
            sys.exit(1)

    def run(coro: Any) -> int:
        return asyncio.run(coro)

    try:
        if args.config_show:
            sys.exit(cmd_config_show(cfg))

        if args.deposit_info:
            print_deposit_info(cfg)
            sys.exit(0)

        if args.calc:
            try:
                price, stars, usdt = _parse_calc_args(args.calc)
            except (ValueError, IndexError) as exc:
                print(f"[ERR] {exc}\nИспользование: --calc СУММА_В_РУБ [ЗВЁЗДЫ] [СЕБЕСТОИМОСТЬ_USDT]")
                sys.exit(1)
            print_profit_calc(cfg, price, stars, usdt)
            sys.exit(0)

        if args.catalog:
            run(show_catalog(cfg))
            sys.exit(0)

        if args.check:
            sys.exit(0 if run(check_system(cfg)) else 1)

        if args.stats:
            hours: list[float] | None = None
            if args.stats_hours:
                try:
                    hours = [float(h) for h in str(args.stats_hours).split(",") if h.strip()]
                except ValueError:
                    print("[ERR] Окна указываются через запятую: --stats-hours 1,6,24")
                    sys.exit(1)
            sys.exit(run(cmd_stats(cfg, hours, as_json=args.json)))

        if args.report:
            sys.exit(run(cmd_report(cfg, as_json=args.json)))

        if args.tasks:
            sys.exit(run(cmd_tasks(cfg, as_json=args.json)))

        if args.timeline:
            sys.exit(run(cmd_timeline(cfg, args.timeline)))

        if args.templates:
            sys.exit(cmd_templates(cfg))

        if args.limits:
            sys.exit(run(cmd_limits(cfg, as_json=args.json)))

        if args.held:
            sys.exit(run(cmd_held(cfg, as_json=args.json)))

        if args.customers:
            sys.exit(run(cmd_customers(cfg, days=args.days, as_json=args.json)))

        if args.blacklist:
            argv = [str(a) for a in args.blacklist]
            action = (argv[0] or "list").lower()
            if action not in ("list", "add", "remove", "rm"):
                print(f"[ERR] Не понимаю «{action}»: нужно list | add @nick | remove @nick")
                sys.exit(1)
            user = argv[1] if len(argv) > 1 else None
            note = " ".join(argv[2:]) if len(argv) > 2 else args.note
            sys.exit(run(cmd_blacklist(cfg, action, user, note=note, as_json=args.json)))

        if args.export:
            fmt = "json" if str(args.export).lower().endswith(".json") else "csv"
            try:
                sys.exit(
                    run(
                        cmd_export(
                            cfg,
                            args.export,
                            fmt=fmt,
                            period=args.since,
                            status=args.export_status,
                            failed=args.failed,
                            buyer=args.buyer,
                            events=args.with_events,
                            limit=args.export_limit,
                            as_json=args.json,
                        )
                    )
                )
            except ValueError as exc:
                print(f"[ERR] {exc}")
                sys.exit(1)

        if args.release:
            try:
                sys.exit(run(cmd_release(cfg, args.release, assume_yes=args.yes, as_json=args.json)))
            except ValueError as exc:
                print(f"[ERR] {exc}")
                sys.exit(1)

        if args.cancel:
            sys.exit(run(cmd_cancel_held(cfg, args.cancel, note=args.note, assume_yes=args.yes)))

        if args.metrics:
            sys.exit(run(cmd_metrics(cfg)))

        if args.order:
            try:
                user, qty, price = _parse_order_args(args.order, cfg, args.order_price)
            except ValueError as exc:
                print(f"[ERR] {exc}\nИспользование: --order @username [ЗВЁЗДЫ]")
                sys.exit(1)
            sys.exit(
                run(
                    cmd_manual_order(
                        cfg,
                        user,
                        qty,
                        price_rub=price,
                        dry_run=args.dry_run,
                        assume_yes=args.yes,
                    )
                )
            )

        if args.stats_push:
            sys.exit(run(cmd_stats_push(cfg)))

        if args.balance:
            sys.exit(run(cmd_balance(cfg, as_json=args.json)))

        if args.pause:
            sys.exit(run(cmd_pause(cfg, pause=True)))

        if args.resume:
            sys.exit(run(cmd_pause(cfg, pause=False)))

        if args.retry_order:
            try:
                cfg.validate()
            except ValueError as exc:
                print(f"[ERR] {exc}")
                sys.exit(1)
            sys.exit(run(cmd_retry(cfg, args.retry_order)))

        if args.test_order:
            if not args.dry_run:
                try:
                    cfg.validate()
                except ValueError as exc:
                    print(f"[ERR] {exc}")
                    sys.exit(1)
            sys.exit(
                run(
                    run_test_order(
                        cfg,
                        args.test_order,
                        args.test_qty,
                        dry_run=args.dry_run,
                        assume_yes=args.yes,
                    )
                )
            )

        try:
            run(run_bot(cfg, once=args.once))
        except (KeyboardInterrupt, SystemExit):
            logger.info("Работа бота остановлена пользователем")
        except ValueError as exc:  # конфигурация — понятный текст вместо трейса
            print(f"[ERR] {exc}")
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
        sys.exit(130)


if __name__ == "__main__":
    main()
