"""Тесты статистики (1ч/2ч/3ч/4ч/6ч/день), трекинга задач, убытков/прибыли/затрат
и расширенного order_processor (ретраи, фактическая себестоимость, reconciliation)."""

from __future__ import annotations

import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autostars.database.db_manager import DBManager
from autostars.services.order_processor import process_paid_order
from autostars.services.statistics import StatisticsService
from autostars.services.task_tracker import (
    EV_COMPLETED,
    EV_RECEIVED,
    TaskTracker,
    reconcile_inflight_orders,
)

RATE_1 = 110.00
RATE_2 = 87.63


# ============================================================================ #
# Хелперы
# ============================================================================ #

async def make_db(tmp_path, name: str = "stats.db") -> DBManager:
    db = DBManager(str(tmp_path / name))
    await db.init_db()
    return db


async def insert_order(db: DBManager, order_id: str, status: str, ts: int,
                       price: float = 0.0, cost_usdt: float = 0.0,
                       cost_rub: float = 0.0, profit: float = 0.0,
                       quantity: int = 1000, username: str = "user",
                       gameau_id: str = "", error: str = "") -> None:
    """Вставка заказа с заданной unix-эпохой (для тестов окон)."""
    conn = await db.get_connection()
    await conn.execute(
        """INSERT INTO orders
           (order_id, gameau_order_id, username, chat_node, quantity, price_rub,
            cost_usdt, cost_rub, profit_rub, status, error, created_ts, updated_ts,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        (order_id, gameau_id, username, "chat_1", quantity, price, cost_usdt,
         cost_rub, profit, status, error, ts, ts),
    )
    await conn.commit()


def minutes_ago(m: float) -> int:
    return int(time.time()) - int(m * 60)


def make_stats(db: DBManager) -> StatisticsService:
    return StatisticsService(db=db, rate_variant_1=RATE_1, rate_variant_2=RATE_2,
                             active_variant=2, tron_energy_fee_rub=0.0)


# ============================================================================ #
# 1. Статистика по окнам 1ч/2ч/3ч/4ч/6ч/24ч/день/всего
# ============================================================================ #

@pytest.mark.anyio
async def test_window_aggregation(tmp_path):
    db = await make_db(tmp_path)
    int(time.time())

    # Заказы в разных точках времени:
    # 30м, 1ч50м (COMPLETED), 3ч30м (FAILED), 4ч20м (PROCESSING),
    # 5ч (COMPLETED), 23ч, 50ч (COMPLETED)
    await insert_order(db, "O-30m", "COMPLETED", minutes_ago(30),
                       price=1370.40, cost_usdt=9.10, profit=572.97)
    await insert_order(db, "O-1h50", "COMPLETED", minutes_ago(110),
                       price=685.20, cost_usdt=4.55, profit=284.66, quantity=500)
    await insert_order(db, "O-3h30", "FAILED", minutes_ago(210),
                       price=1370.40, error="NETWORK_TIMEOUT")
    await insert_order(db, "O-4h20", "PROCESSING", minutes_ago(260))
    await insert_order(db, "O-5h", "COMPLETED", minutes_ago(300),
                       price=685.20, cost_usdt=4.55, profit=284.66, quantity=500)
    await insert_order(db, "O-23h", "COMPLETED", minutes_ago(1380),
                       price=1370.40, cost_usdt=9.10, profit=572.97)
    await insert_order(db, "O-50h", "COMPLETED", minutes_ago(3000),
                       price=1370.40, cost_usdt=9.10, profit=572.97)

    stats = make_stats(db)
    windows = {w.label: w for w in await stats.compute_all()}

    w1h = windows["1 час"]
    assert w1h.total == 1 and w1h.completed == 1
    assert w1h.revenue_rub == pytest.approx(1370.40)
    assert w1h.cost_usdt == pytest.approx(9.10)
    assert w1h.profit_rub == pytest.approx(572.97)
    assert w1h.stars == 1000
    # себестоимость в рублях по активному курсу (Вариант 2 = 87.63)
    assert w1h.cost_rub == pytest.approx(9.10 * RATE_2, abs=0.01)

    w2h = windows["2 часа"]
    assert w2h.total == 2  # 30м + 110м
    assert w2h.profit_rub == pytest.approx(572.97 + 284.66)

    w3h = windows["3 часа"]
    assert w3h.total == 2  # 3ч30м уже за окном 3ч

    w4h = windows["4 часа"]
    assert w4h.total == 3  # + FAILED 3ч30м

    w6h = windows["6 часов"]
    assert w6h.total == 5  # + PROCESSING 4ч20м + COMPLETED 5ч
    assert w6h.in_progress == 1
    assert w6h.stars == 1000 + 500 + 500  # звёзды только по completed

    w24h = windows["24 часа"]
    assert w24h.total == 6  # всё кроме 50ч
    assert w24h.failed == 1
    assert w24h.failed_potential_rub == pytest.approx(1370.40)  # потерянная выручка

    total = windows["Всего"]
    assert total.total == 7
    assert total.completed == 5
    assert total.failed == 1
    assert total.in_progress == 1
    assert total.revenue_rub == pytest.approx(1370.40 * 3 + 685.20 * 2)
    assert total.best_profit_rub == pytest.approx(572.97)
    assert total.avg_profit_rub == pytest.approx(
        (572.97 * 3 + 284.66 * 2) / 5, abs=0.01
    )

    # margin: прибыль / выручка
    assert total.margin_pct == pytest.approx(
        total.profit_rub / total.revenue_rub * 100, abs=0.1
    )

    # Кастомный набор окон
    custom = await stats.window("последние 25 ч", 25)
    assert custom.total == 6

    await db.close()


@pytest.mark.anyio
async def test_today_and_total_windows(tmp_path):
    db = await make_db(tmp_path)

    now_dt = datetime.now()
    today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_mid = today_start - timedelta(hours=12)

    # вчера (за гарантированно за календарным днём)
    await insert_order(db, "O-YESTERDAY", "COMPLETED", int(yesterday_mid.timestamp()),
                       price=100.0, profit=20.0)
    # сегодня, сразу после полуночи
    await insert_order(db, "O-TODAY", "COMPLETED", int(today_start.timestamp()) + 60,
                       price=500.0, profit=100.0)

    stats = make_stats(db)
    today = await stats.today_window()
    total = await stats.total_window()

    assert today.total == 1
    assert today.revenue_rub == pytest.approx(500.0)
    assert total.total == 2
    assert total.revenue_rub == pytest.approx(600.0)

    await db.close()


@pytest.mark.anyio
async def test_cost_rub_recomputed_for_legacy_rows(tmp_path):
    """Старые записи без cost_rub: себестоимость пересчитывается по курсу."""
    db = await make_db(tmp_path)
    # cost_rub = 0 — имитация записи до v2.1
    await insert_order(db, "O-LEGACY", "COMPLETED", minutes_ago(10),
                       price=1370.40, cost_usdt=9.10, cost_rub=0.0, profit=572.97)
    stats = make_stats(db)
    w = await stats.window("тест", 1)
    assert w.cost_rub == pytest.approx(9.10 * RATE_2, abs=0.01)

    # Трон-комиссия считается по числу completed
    stats_fee = StatisticsService(db=db, rate_variant_1=RATE_1, rate_variant_2=RATE_2,
                                  active_variant=2, tron_energy_fee_rub=2.0)
    w_fee = await stats_fee.window("тест", 1)
    assert w_fee.cost_rub == pytest.approx(9.10 * RATE_2 + 2.0, abs=0.01)
    await db.close()


# ============================================================================ #
# 2. Анализ убытков
# ============================================================================ #

@pytest.mark.anyio
async def test_loss_report(tmp_path):
    db = await make_db(tmp_path)
    await insert_order(db, "O-OK", "COMPLETED", minutes_ago(30),
                       price=1370.40, cost_usdt=9.10, profit=572.97)
    await insert_order(db, "O-LOSS", "COMPLETED", minutes_ago(60),
                       price=600.0, cost_usdt=9.10, profit=600.0 - 9.10 * RATE_2)
    await insert_order(db, "O-FAIL1", "FAILED", minutes_ago(90), price=1370.40)
    await insert_order(db, "O-FAIL2", "FAILED_LOW_BALANCE", minutes_ago(120),
                       price=685.20, error="LOW_BALANCE")

    stats = make_stats(db)
    report = await stats.loss_report(hours=24)

    assert report["failed_count"] == 2
    assert report["failed_potential_rub"] == pytest.approx(1370.40 + 685.20)
    assert report["loss_orders_count"] == 1
    assert report["loss_rub"] == pytest.approx(600.0 - 9.10 * RATE_2, abs=0.01)
    assert len(report["failed_orders"]) == 2
    assert report["failed_orders"][0]["status"] in ("FAILED", "FAILED_LOW_BALANCE")
    neg = report["negative_profit_orders"]
    assert len(neg) == 1 and neg[0]["order_id"] == "O-LOSS"

    await db.close()


# ============================================================================ #
# 3. Трекинг выполнения задач
# ============================================================================ #

@pytest.mark.anyio
async def test_task_tracker_timeline_and_stuck(tmp_path):
    db = await make_db(tmp_path)
    tracker = TaskTracker(db)

    # Свежая задача
    await insert_order(db, "T-FRESH", "PROCESSING", minutes_ago(5), gameau_id="G-1")
    # Зависшая задача (без движения 40 минут)
    await insert_order(db, "T-STUCK", "PROCESSING", minutes_ago(40), gameau_id="G-2")

    # Таймлайн
    await tracker.log("T-FRESH", EV_RECEIVED, "price=1370.4₽")
    await tracker.log("T-FRESH", EV_COMPLETED, "profit=572.97₽")
    tl = await tracker.timeline("T-FRESH")
    assert [e["event"] for e in tl] == [EV_RECEIVED, EV_COMPLETED]
    assert tl[0]["detail"] == "price=1370.4₽"

    # Открытые и зависшие
    open_tasks = await tracker.open_tasks()
    assert {t["order_id"] for t in open_tasks} == {"T-FRESH", "T-STUCK"}

    stuck = await tracker.stuck_tasks(stuck_minutes=20)
    assert [t["order_id"] for t in stuck] == ["T-STUCK"]

    # Анти-спам: разовый алерт
    assert await db.has_recent_task_event("T-FRESH", "STUCK_ALERTED") is False
    await db.log_task_event("T-FRESH", "STUCK_ALERTED", "test")
    assert await db.has_recent_task_event("T-FRESH", "STUCK_ALERTED") is True

    # Сводка статусов
    counts = await tracker.summary()
    assert counts.get("PROCESSING") == 2

    # Рендер текста не падает
    text = await tracker.render_text(stuck_minutes=20)
    assert "ТРЕКИНГ" in text and "T-STUCK" in text

    await db.close()


# ============================================================================ #
# 4. Расширенный order_processor
# ============================================================================ #

class RetryMockGameau:
    """Успех только с N-й попытки (сетевой сбой до этого)."""

    def __init__(self, fail_times: int = 2):
        self.fail_times = fail_times
        self.calls = 0

    async def buy_telegram_stars(self, username, quantity, order_id,
                                 max_charge_usdt=9.50, hide_sender=False):
        self.calls += 1
        if self.calls <= self.fail_times:
            return {"success": False, "error": "NETWORK_TIMEOUT"}
        idem = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))
        return {
            "success": True,
            "data": {"orderId": "G-RETRY", "chargedAmount": 9.10},
            "idempotency_key": idem,
        }

    async def find_stars_package(self, stars):
        return {"name": f"Telegram Stars {stars}", "price": 9.10, "order": {"body": {"quantity": stars}}}


class MockFunPayClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, node, text):
        self.sent.append((node, text))
        return True

    async def get_last_buyer_message(self, node):
        return None


class MockNotifier:
    def __init__(self):
        self.alerts = []
        self.current_rate = RATE_2

    def calculate_profit(self, order_price_rub, usdt_cost, rate=None, tron_energy_rub=None):
        r = rate or self.current_rate
        return round(order_price_rub - (usdt_cost * r), 2)

    async def send_alert(self, text, parse_mode="HTML", *, critical=True):
        self.alerts.append(text)
        return True

    async def alert_order_completed(self, order_id, username, profit_rub=0.0,
                                    order_price_rub=None, usdt_cost=9.10):
        self.alerts.append(f"COMPLETED #{order_id} @{username} +{profit_rub}₽")
        return True

    async def alert_low_balance(self, order_id):
        self.alerts.append(f"LOW_BALANCE #{order_id}")
        return True

    async def alert_price_exceeded(self, order_id, max_charge):
        self.alerts.append(f"PRICE_EXCEEDED #{order_id} {max_charge}")
        return True


@pytest.mark.anyio
async def test_processor_retries_then_succeeds(tmp_path):
    db = await make_db(tmp_path)
    fp, tg = MockFunPayClient(), MockNotifier()
    gameau = RetryMockGameau(fail_times=2)

    res = await process_paid_order(
        {"id": "FP-R1", "chat_node": "c1", "last_message": "@durov",
         "price": 1370.40, "quantity": 1000},
        fp, gameau, db, tg,
        max_order_retries=2,
        task_tracker=TaskTracker(db),
    )

    assert res["status"] == "completed"
    assert gameau.calls == 3  # 2 сбоя + успех
    # Журнал задач зафиксировал ретраи
    tl = await db.get_task_timeline("FP-R1")
    events = [e["event"] for e in tl]
    assert events.count("RETRY") == 2
    assert EV_COMPLETED in events
    assert "GAMEAU_CREATED" in events
    await db.close()


@pytest.mark.anyio
async def test_processor_uses_charged_amount(tmp_path):
    """Себестоимость берётся из chargedAmount ответа GAMEAU, а не 9.10."""
    db = await make_db(tmp_path)
    fp, tg = MockFunPayClient(), MockNotifier()

    class ChargedGameau:
        async def buy_telegram_stars(self, username, quantity, order_id,
                                     max_charge_usdt=9.50, hide_sender=False):
            idem = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))
            return {"success": True, "data": {"orderId": "G-CHG", "chargedAmount": 8.95},
                    "idempotency_key": idem}

    res = await process_paid_order(
        {"id": "FP-CHG", "chat_node": "c1", "last_message": "@durov",
         "price": 1370.40, "quantity": 1000},
        fp, ChargedGameau(), db, tg,
    )

    assert res["status"] == "completed"
    row = await db.get_order("FP-CHG")
    assert row["cost_usdt"] == pytest.approx(8.95)
    expected_profit = round(1370.40 - 8.95 * RATE_2, 2)
    assert row["profit_rub"] == pytest.approx(expected_profit)
    assert row["gameau_order_id"] == "G-CHG"
    assert row["cost_rub"] == pytest.approx(8.95 * RATE_2, abs=0.01)
    await db.close()


@pytest.mark.anyio
async def test_processor_gameau_delivery_failed(tmp_path):
    """GAMEAU принял заказ, но финальный статус — failed (звёзды не выданы)."""
    db = await make_db(tmp_path)
    fp, tg = MockFunPayClient(), MockNotifier()

    class FailedDeliveryGameau:
        async def buy_telegram_stars(self, username, quantity, order_id,
                                     max_charge_usdt=9.50, hide_sender=False):
            idem = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))
            return {"success": True, "data": {"orderId": "G-FAIL", "amount": 9.10},
                    "idempotency_key": idem}

        async def wait_for_completion(self, order_id, poll_interval=3.0, timeout=120.0):
            raise RuntimeError("Заказ Gameau G-FAIL завершился со статусом 'failed'")

    res = await process_paid_order(
        {"id": "FP-DF", "chat_node": "c1", "last_message": "@durov",
         "price": 1370.40, "quantity": 1000},
        fp, FailedDeliveryGameau(), db, tg,
    )

    assert res["status"] == "failed"
    row = await db.get_order("FP-DF")
    assert row["status"] == "FAILED_DELIVERY"
    assert row["cost_usdt"] == pytest.approx(9.10)  # деньги списаны, звёзд нет
    # Покупателю ответ НЕ отправлен, владельцу — алерт об ошибке с оценкой убытка
    assert len(fp.sent) == 0
    assert any("ПОТЕРЯННАЯ" in a.upper() or "потерянная выручка" in a for a in tg.alerts)
    await db.close()


@pytest.mark.anyio
async def test_processor_unconfirmed_delivery_stays_in_progress(tmp_path):
    """Не подтверждённая выдача (timeout/unknown) не ставит COMPLETED:
    задача остаётся в работе для reconciliation."""
    db = await make_db(tmp_path)
    fp, tg = MockFunPayClient(), MockNotifier()

    class TimeoutGameau:
        async def buy_telegram_stars(self, username, quantity, order_id,
                                     max_charge_usdt=9.50, hide_sender=False):
            idem = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))
            return {"success": True, "data": {"orderId": "G-TO", "status": "processing"},
                    "idempotency_key": idem}

        async def wait_for_completion(self, order_id, poll_interval=3.0, timeout=120.0):
            return {"id": order_id, "status": "timeout"}

    res = await process_paid_order(
        {"id": "FP-TO", "chat_node": "c1", "last_message": "@durov",
         "price": 1370.40, "quantity": 1000},
        fp, TimeoutGameau(), db, tg,
        wait_completion_timeout=10,
    )

    assert res["status"] == "unconfirmed"
    row = await db.get_order("FP-TO")
    assert row["status"] == "PROCESSING"  # не COMPLETED!
    assert row["gameau_order_id"] == "G-TO"
    # покупателю НЕ отправлено подтверждение, владельцу — предупреждение
    assert len(fp.sent) == 0
    assert any("ЗАДАЧА В РАБОТЕ" in a for a in tg.alerts)
    # reconciliation сможет подхватить задачу
    assert any(o["order_id"] == "FP-TO" for o in await TaskTracker(db).open_tasks())
    await db.close()


# ============================================================================ #
# 5. Авто-согласование (reconciliation) незавершённых задач
# ============================================================================ #

class ReconcileMockGameau:
    def __init__(self, final_status: str = "completed"):
        self.final_status = final_status
        self.status_calls = 0

    async def get_order_status(self, order_id):
        self.status_calls += 1
        return {"id": order_id, "status": self.final_status, "chargedAmount": 9.10}


class ReconcileMockNotifier(MockNotifier):
    def __init__(self):
        super().__init__()
        self.stuck_alerts = 0

    async def send_alert(self, text, parse_mode="HTML", *, critical=True):
        self.alerts.append(text)
        if "ЗАВИСШАЯ ЗАДАЧА" in text:
            self.stuck_alerts += 1
        return True


@pytest.mark.anyio
async def test_reconcile_completes_inflight_order(tmp_path):
    db = await make_db(tmp_path)
    # Незавершённая задача с известным GAMEAU ID
    await insert_order(db, "RC-1", "PROCESSING", minutes_ago(2),
                       price=1370.40, quantity=1000, username="durov",
                       gameau_id="G-RC-1")
    await db.save_order_status("RC-1", "durov", "PROCESSING", chat_node="chat_rc",
                               quantity=1000, price_rub=1370.40,
                               gameau_order_id="G-RC-1")

    tracker = TaskTracker(db)
    fp = MockFunPayClient()
    gameau = ReconcileMockGameau("completed")
    tg = ReconcileMockNotifier()

    finished = await reconcile_inflight_orders(
        tracker=tracker, db=db, gameau_client=gameau,
        funpay_client=fp, tg_notifier=tg, stuck_minutes=20,
    )

    assert finished == 1
    row = await db.get_order("RC-1")
    assert row["status"] == "COMPLETED"
    assert row["profit_rub"] == pytest.approx(round(1370.40 - 9.10 * RATE_2, 2))
    # Покупателю отправлено подтверждение, владельцу — алерт
    assert len(fp.sent) == 1
    assert any("COMPLETED #RC-1" in a for a in tg.alerts)
    # Журнал событий
    tl = await tracker.timeline("RC-1")
    assert any(e["event"] == "GAMEAU_COMPLETED" for e in tl)
    await db.close()


@pytest.mark.anyio
async def test_reconcile_stuck_alert_once(tmp_path):
    db = await make_db(tmp_path)
    await insert_order(db, "RC-STUCK", "PROCESSING", minutes_ago(50),
                       price=1370.40, quantity=1000, username="durov",
                       gameau_id="G-RC-S")
    conn = await db.get_connection()
    await conn.execute("UPDATE orders SET updated_ts = ? WHERE order_id = 'RC-STUCK'",
                       (minutes_ago(50),))
    await conn.commit()

    tracker = TaskTracker(db)
    fp = MockFunPayClient()
    # Статус остаётся processing — но алерт о зависании должен уйти
    gameau = ReconcileMockGameau("processing")
    tg = ReconcileMockNotifier()

    await reconcile_inflight_orders(tracker, db, gameau, fp, tg, stuck_minutes=20)
    assert tg.stuck_alerts == 1

    # Повторный проход — алерт не дублируется (анти-спам 60 мин)
    await reconcile_inflight_orders(tracker, db, gameau, fp, tg, stuck_minutes=20)
    assert tg.stuck_alerts == 1

    # Задача по-прежнему в работе
    row = await db.get_order("RC-STUCK")
    assert row["status"] == "PROCESSING"
    await db.close()


# ============================================================================ #
# 6. Миграция legacy-баз
# ============================================================================ #

@pytest.mark.anyio
async def test_legacy_db_migration(tmp_path):
    """Базы v2.0 (без новых колонок) прозрачно мигрируются."""
    import aiosqlite

    db_file = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(db_file) as conn:
        await conn.execute(
            """CREATE TABLE orders (
                order_id TEXT PRIMARY KEY, username TEXT, chat_node TEXT,
                quantity INTEGER DEFAULT 1000, price_rub REAL DEFAULT 0.0,
                cost_usdt REAL DEFAULT 0.0, profit_rub REAL DEFAULT 0.0,
                status TEXT NOT NULL, error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await conn.execute(
            "INSERT INTO orders (order_id, username, status, price_rub, cost_usdt, profit_rub) "
            "VALUES ('OLD-1', 'legacy', 'COMPLETED', 1370.40, 9.10, 572.97)"
        )
        await conn.commit()

    db = DBManager(db_file)
    await db.init_db()

    # Новая колонка появилась, старые данные на месте
    row = await db.get_order("OLD-1")
    assert row["price_rub"] == pytest.approx(1370.40)
    assert "gameau_order_id" in row
    assert "cost_rub" in row
    assert "created_ts" in row

    # Статистика работает и с legacy-строками (created_at -> epoch)
    stats = make_stats(db)
    total = await stats.total_window()
    assert total.total == 1
    assert total.revenue_rub == pytest.approx(1370.40)
    assert total.cost_rub == pytest.approx(9.10 * RATE_2, abs=0.01)

    await db.close()


# ============================================================================ #
# 7. Рендеринг отчётов
# ============================================================================ #

@pytest.mark.anyio
async def test_report_rendering(tmp_path):
    db = await make_db(tmp_path)
    await insert_order(db, "R-1", "COMPLETED", minutes_ago(10),
                       price=1370.40, cost_usdt=9.10, profit=572.97)
    await insert_order(db, "R-2", "FAILED", minutes_ago(30), price=1370.40)
    # за 3 часа — вне 1-часового окна, чтобы прибыль 1ч-окна была 572.97
    await insert_order(db, "R-3", "COMPLETED", minutes_ago(180),
                       price=600.0, cost_usdt=9.10, profit=round(600.0 - 9.10 * RATE_2, 2))

    stats = make_stats(db)
    windows = await stats.compute_all()

    text = stats.render_text(windows)
    for label in ("1 час", "2 часа", "3 часа", "4 часа", "6 часов", "24 часа",
                  "Сегодня (с 00:00)", "Всего"):
        assert label in text, f"нет окна {label} в отчёте"

    tg_text = stats.render_telegram(windows)
    assert "СТАТИСТИКА AUTOSTARS" in tg_text
    assert "572.97" in tg_text  # прибыль 1-часового окна
    assert "87.63" in tg_text  # курс

    report = stats.render_full_report(
        windows,
        top_orders=await db.top_orders(limit=5, start_ts=minutes_ago(1440)),
        failed_orders=await db.top_orders(limit=5, start_ts=minutes_ago(1440), failed_only=True),
        status_counts=await db.get_status_counts(),
    )
    assert "P&L" in report
    assert "Топ сделок" in report
    assert "Проваленные заказы" in report
    assert "R-2" in report

    await db.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
