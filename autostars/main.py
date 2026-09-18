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

В живом цикле доступны TG-команды владельца: /help /status /stats /report /tasks
/balance /pause /resume /retry <id> /calc <цена> [звёзды].

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
import json
import logging
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

        last = {"reconcile": 0.0, "stats_push": 0.0, "chat_monitor": 0.0, "balance": time.time()}
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
                            await bot_control.check_gameau_balance(gameau, notifier, cfg.low_balance_threshold_usdt, alert=True)
                        except Exception as exc:
                            logger.error(f"Ошибка проверки баланса GAMEAU: {exc}", exc_info=True)

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
        if not (args.check or args.calc or args.config_show):
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
