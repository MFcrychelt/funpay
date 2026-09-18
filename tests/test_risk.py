"""Политика выдачи (защита от убытка): лимиты, дубли, стоп-лист, ручное решение.

Это самый важный слой с точки зрения денег: он решает, НАЖИМАТЬ ли «купить».
Тесты потому и мокают GAMEAU полностью — ни один тест не должен отправить
реальный запрос и списать USDT.

Что проверяется:
  • `evaluate_limits` — три проверки (крупная сделка, маржа, суточный бюджет);
  • `action_for_duplicates` / `summarize` — что alert не блокирует, а hold блокирует;
  • `evaluate_order_risk` — дубликаты и стоп-лист по реальной SQLite-базе;
  • `process_paid_order` — заказ задержан ДО покупки (ни одного вызова GAMEAU),
    `ignore_policy`/`policy=None` — прежний путь, шаблон `hold` уходит покупателю;
  • `bot_control.retry_failed_order` — HOLD_MANUAL отпускается и выдаётся;
  • CLI `--limits/--held/--cancel/--blacklist/--customers` и TG `/limits /held
    /blacklist /customers` (с фикстуры `isolated_env`, без сети).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autostars.config import Config
from autostars.database.db_manager import DBManager
from autostars.services import policy as policy_mod
from autostars.services.bot_control import retry_failed_order
from autostars.services.order_processor import process_paid_order
from autostars.services.policy import (
    ACTION_ALERT,
    ACTION_HOLD,
    ACTION_IGNORE,
    HOLD_STATUS,
    OrderPolicy,
    ReplyTemplates,
    action_for_duplicates,
    alert_text,
    evaluate_limits,
    summarize,
)
from autostars.services.task_tracker import TaskTracker

ROOT = Path(__file__).resolve().parents[1]

RATE = 87.63


def make_policy(**overrides) -> OrderPolicy:
    base = {"rate_rub_per_usdt": RATE}
    base.update(overrides)
    return OrderPolicy(**base)


# ============================================================================ #
# 1. Локальные проверки (без базы и без сети)
# ============================================================================ #


def test_zero_limits_mean_no_checks():
    """0 = выключено: поведение прежней версии, ни одного предупреждения."""
    pol = make_policy(blacklist_enabled=False)
    assert pol.any_check_on is False
    assert evaluate_limits(pol, price_rub=1_000_000, cost_rub=1) == []


def test_large_order_is_held():
    pol = make_policy(max_order_revenue_rub=5000)
    checks = evaluate_limits(pol, price_rub=6852.0, cost_rub=45.5 * RATE)
    assert [c.code for c in checks] == ["LARGE_ORDER"]
    assert checks[0].action == ACTION_HOLD
    assert "6852" in checks[0].reason


def test_large_order_below_limit_passes():
    pol = make_policy(max_order_revenue_rub=5000)
    assert evaluate_limits(pol, price_rub=4999.9, cost_rub=100.0) == []


def test_low_margin_holds_and_good_margin_passes():
    pol = make_policy(min_margin_pct=20)
    # выручка 1370.40, затраты 1 200 ₽ → маржа ~12.4% < 20%
    bad = evaluate_limits(pol, price_rub=1370.40, cost_rub=1200.0)
    assert [c.code for c in bad] == ["LOW_MARGIN"]
    good = evaluate_limits(pol, price_rub=1370.40, cost_rub=9.1 * RATE)
    assert good == []


def test_daily_budget_counts_next_purchase_not_only_spent():
    """Лимит сравнивается с «уже + ещё», иначе последний заказ всегда проходит."""
    pol = make_policy(daily_spend_limit_usdt=100)
    ok = evaluate_limits(pol, price_rub=1000, cost_rub=100, spent_today_usdt=95.0, order_cost_usdt=4.0)
    assert ok == []
    over = evaluate_limits(pol, price_rub=1000, cost_rub=100, spent_today_usdt=95.0, order_cost_usdt=9.0)
    assert [c.code for c in over] == ["DAILY_LIMIT"]
    assert "104.00" in over[0].reason


def test_several_checks_reported_together():
    pol = make_policy(max_order_revenue_rub=1000, min_margin_pct=30, daily_spend_limit_usdt=10)
    checks = evaluate_limits(pol, price_rub=2000, cost_rub=1900, spent_today_usdt=9.0, order_cost_usdt=9.1)
    assert {c.code for c in checks} == {"LARGE_ORDER", "LOW_MARGIN", "DAILY_LIMIT"}


def test_duplicate_action_falls_back_to_alert_on_garbage():
    assert action_for_duplicates(make_policy(duplicate_action="hold")) == ACTION_HOLD
    assert action_for_duplicates(make_policy(duplicate_action="ignore")) == ACTION_IGNORE
    assert action_for_duplicates(make_policy(duplicate_action="что-угодно")) == ACTION_ALERT


def test_summarize_prefers_hold_but_keeps_alerts_visible():
    alerts = [
        policy_mod.RiskDecision(code="DUPLICATE", action=ACTION_ALERT, reason="дубль"),
        policy_mod.RiskDecision(code="LARGE_ORDER", action=ACTION_HOLD, reason="крупный"),
    ]
    hold, shown = summarize(alerts)
    assert hold is not None and hold.code == "LARGE_ORDER"
    assert len(shown) == 2
    # «ignore» не должен попадать в сообщение владельцу
    hold2, shown2 = summarize([policy_mod.RiskDecision(code="X", action=ACTION_IGNORE, reason="y")])
    assert hold2 is None and shown2 == []


def test_day_start_is_midnight():
    pol = make_policy()
    now = time.time()
    start = pol.day_start_ts(now)
    assert start <= now
    assert now - start < 86400
    assert time.localtime(start).tm_hour == 0 and time.localtime(start).tm_min == 0


def test_alert_text_mentions_release_and_reason():
    text = alert_text(
        "42",
        "durov",
        1000,
        1370.4,
        [policy_mod.RiskDecision(code="LARGE_ORDER", action=ACTION_HOLD, reason="больше лимита")],
        held=True,
    )
    assert "ЗАДЕРЖАН" in text and "/release 42" in text and "--cancel 42" in text
    assert "LARGE_ORDER" in text and "@durov" in text


# ============================================================================ #
# 2. Проверки по реальной базе (SQLite во временном каталоге)
# ============================================================================ #


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def new_db(tmp_path: Path, name: str = "risk.db") -> DBManager:
    """База во временном каталоге (CLI использует autostars.db — см. `name`)."""
    db = DBManager(str(tmp_path / name))
    await db.init_db()
    return db


@pytest.mark.anyio
async def test_blacklist_holds_order(tmp_path):
    db = await new_db(tmp_path)
    await db.add_buyer_flag("@mallory", note="не платит")
    assert await db.is_buyer_flagged("mallory")
    assert await db.is_buyer_flagged("@mallory")
    assert not await db.is_buyer_flagged("durov")
    checks = await policy_mod.evaluate_order_risk(
        make_policy(blacklist_enabled=True),
        db,
        order_id="1",
        username="@mallory",
        quantity=1000,
        price_rub=1000.0,
        cost_rub=800.0,
        order_cost_usdt=9.1,
    )
    assert [c.code for c in checks] == ["BLACKLISTED"]
    assert checks[0].action == ACTION_HOLD
    # выключенный стоп-лист не должен ничего решать
    off = await policy_mod.evaluate_order_risk(
        make_policy(blacklist_enabled=False),
        db,
        order_id="1",
        username="@mallory",
        quantity=1000,
        price_rub=1000.0,
        cost_rub=800.0,
        order_cost_usdt=9.1,
    )
    assert off == []
    assert await db.remove_buyer_flag("@mallory") == 1
    assert not await db.is_buyer_flagged("@mallory")
    await db.close()


@pytest.mark.anyio
async def test_duplicate_detection_uses_window_and_quantity(tmp_path):
    db = await new_db(tmp_path)
    now = int(time.time())
    await db.save_order_status(
        "OLD-1", "@bob", "COMPLETED", quantity=1000, price_rub=1370.4, chat_node="c1"
    )
    conn = await db.get_connection()
    # свежий заказ того же ника и количества — «внутри» окна
    await conn.execute(
        "UPDATE orders SET created_ts = ?, updated_ts = ? WHERE order_id = 'OLD-1'",
        (now - 120, now - 120),
    )
    await conn.commit()

    inside = await policy_mod.evaluate_order_risk(
        make_policy(duplicate_window_min=15, duplicate_action="hold", blacklist_enabled=False),
        db,
        order_id="NEW-1",
        username="@bob",
        quantity=1000,
        price_rub=1370.4,
        cost_rub=797.43,
        order_cost_usdt=9.1,
    )
    assert [c.code for c in inside] == ["DUPLICATE"]
    assert inside[0].action == ACTION_HOLD
    assert "OLD-1" in inside[0].reason

    # другое количество — не дубль
    other_qty = await policy_mod.evaluate_order_risk(
        make_policy(duplicate_window_min=15, blacklist_enabled=False),
        db,
        order_id="NEW-2",
        username="@bob",
        quantity=2000,
        price_rub=2740.8,
        cost_rub=1594.86,
        order_cost_usdt=18.2,
    )
    assert other_qty == []

    # старый заказ вне окна — не дубль
    await conn.execute("UPDATE orders SET created_ts = ? WHERE order_id = 'OLD-1'", (now - 7200,))
    await conn.commit()
    stale = await policy_mod.evaluate_order_risk(
        make_policy(duplicate_window_min=15, blacklist_enabled=False),
        db,
        order_id="NEW-3",
        username="@bob",
        quantity=1000,
        price_rub=1370.4,
        cost_rub=797.43,
        order_cost_usdt=9.1,
    )
    assert stale == []
    await db.close()


@pytest.mark.anyio
async def test_daily_spend_uses_cost_of_non_cancelled_orders(tmp_path):
    db = await new_db(tmp_path)
    pol = make_policy(daily_spend_limit_usdt=15)
    await db.save_order_status("A", "@a", "COMPLETED", cost_usdt=9.1, quantity=1000, price_rub=1370.4)
    await db.save_order_status("B", "@b", "CANCELLED", cost_usdt=15.0, quantity=1000, price_rub=1370.4)
    spent = await db.sum_cost_usdt_since(pol.day_start_ts())
    assert spent == pytest.approx(9.1)
    checks = await policy_mod.evaluate_order_risk(
        pol,
        db,
        order_id="C",
        username="@c",
        quantity=1000,
        price_rub=1370.4,
        cost_rub=797.43,
        order_cost_usdt=9.1,
    )
    assert [c.code for c in checks] == ["DAILY_LIMIT"]
    await db.close()


@pytest.mark.anyio
async def test_held_status_counted_in_window_and_selectable(tmp_path):
    db = await new_db(tmp_path)
    await db.save_order_status("H-1", "@h", HOLD_STATUS, quantity=1000, price_rub=100.0, error="LARGE_ORDER: x")
    stats = await db.window_stats(start_ts=int(time.time()) - 60)
    assert stats["held"] == 1
    assert stats["completed"] == 0
    rows = await db.select_orders(statuses=[HOLD_STATUS], limit=5)
    assert [r["order_id"] for r in rows] == ["H-1"]
    assert rows[0]["error"] == "LARGE_ORDER: x"
    # фильтр по покупателю (с @ и без) и периоду
    assert [r["order_id"] for r in await db.select_orders(username="@h")] == ["H-1"]
    assert await db.select_orders(username="someone-else") == []
    assert await db.select_orders(since="2099-01-01 00:00:00") == []
    await db.close()


@pytest.mark.anyio
async def test_buyer_stats_aggregates(tmp_path):
    db = await new_db(tmp_path)
    for i, (status, price, profit) in enumerate(
        [("COMPLETED", 1370.4, 572.97), ("FAILED", 100.0, 0.0), ("COMPLETED", 2740.8, 1145.94)]
    ):
        await db.save_order_status(
            f"S-{i}", "@durov", status, quantity=1000, price_rub=price, profit_rub=profit, cost_usdt=9.1
        )
    rows = await db.buyer_stats(int(time.time()) - 3600, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["orders"] == 3 and row["completed"] == 2 and row["failed"] == 1
    assert row["revenue_rub"] == pytest.approx(4211.2)
    assert row["profit_rub"] == pytest.approx(1718.91)
    await db.close()


# ============================================================================ #
# 3. Пайплайн: задержание ДО покупки и обход политики человеком
# ============================================================================ #


class RecordingGameau:
    """Никакой сети: единственная запись — «бот вообще собрался покупать»."""

    def __init__(self, charged: float = 9.10):
        self.calls: list[dict] = []
        self.charged = charged

    async def find_stars_package(self, quantity: int):
        return None

    async def buy_telegram_stars(self, username: str, quantity: int, order_id: str, **_kwargs):
        self.calls.append({"username": username, "quantity": quantity, "order_id": order_id})
        return {
            "success": True,
            "data": {"orderId": f"G-{order_id}", "status": "completed", "chargedAmount": self.charged},
        }

    async def get_order(self, gameau_order_id: str):
        return {"success": True, "data": {"orderId": gameau_order_id, "status": "completed"}}


class RecordingFunPay:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, node: str, text: str, hide_sender: bool = False) -> bool:
        self.sent.append((node, text))
        return True


class RecordingNotifier:
    def __init__(self):
        self.alerts: list[tuple[str, bool]] = []
        self.current_rate = RATE

    def calculate_profit(self, order_price_rub, usdt_cost, rate=None, tron_energy_rub=None):
        return round(order_price_rub - usdt_cost * (rate or RATE), 2)

    async def send_alert(self, text, parse_mode="HTML", *, critical=True):
        self.alerts.append((text, critical))
        return True

    async def alert_order_completed(self, order_id, username, **_kw):
        return True

    async def alert_low_balance(self, order_id):
        return True

    async def alert_price_exceeded(self, order_id, max_charge):
        return True


def order_payload(order_id: str = "500", username: str = "durov", quantity: int = 1000, price: float = 1370.4):
    return {
        "id": order_id,
        "chat_node": f"chat-{order_id}",
        "last_message": f"@{username} {quantity} stars",
        "description": f"{quantity} Stars",
        "quantity": quantity,
        "price": price,
    }


async def run_pipeline(db, *, policy, gameau=None, notifier=None, ignore_policy=False, order=None,
                       bypass_processed_check=False):
    funpay = RecordingFunPay()
    gameau = gameau or RecordingGameau()
    notifier = notifier or RecordingNotifier()
    result = await process_paid_order(
        order_data=order or order_payload(),
        funpay_client=funpay,
        gameau_client=gameau,
        db=db,
        tg_notifier=notifier,
        task_tracker=TaskTracker(db),
        policy=policy,
        ignore_policy=ignore_policy,
        bypass_processed_check=bypass_processed_check,
        usdt_per_1000_stars=9.10,
        max_charge_usdt=9.5,
        whitebird_rate=RATE,
    )
    return result, funpay, gameau, notifier


@pytest.mark.anyio
async def test_hold_blocks_purchase_and_writes_status(tmp_path):
    db = await new_db(tmp_path)
    pol = make_policy(max_order_revenue_rub=500)
    result, funpay, gameau, notifier = await run_pipeline(
        db, policy=pol, order=order_payload(price=1370.4)
    )
    assert result["status"] == "held" and result["code"] == "LARGE_ORDER"
    assert gameau.calls == [], "покупка не должна была даже начаться"
    row = await db.get_order("500")
    assert row["status"] == HOLD_STATUS
    assert row["error"].startswith("LARGE_ORDER:")
    assert row["username"] == "durov"
    # владелец получил КРИТИЧНЫЙ алерт (его нельзя потерять в тихие часы)
    assert notifier.alerts and notifier.alerts[0][1] is True
    assert "LARGE_ORDER" in notifier.alerts[0][0]
    assert funpay.sent == [], "без шаблона hold покупателю не пишем"
    tl = await TaskTracker(db).timeline("500")
    assert any(e["event"] == "RISK_HOLD" for e in tl)
    await db.close()


@pytest.mark.anyio
async def test_hold_sends_template_to_buyer(tmp_path):
    db = await new_db(tmp_path)
    pol = OrderPolicy(
        max_order_revenue_rub=500,
        rate_rub_per_usdt=RATE,
        templates=ReplyTemplates(hold="Заказ #{order_id} на проверке, продавец ответит shortly."),
    )
    result, funpay, gameau, _ = await run_pipeline(db, policy=pol, order=order_payload(price=900.0))
    assert result["status"] == "held"
    assert gameau.calls == []
    assert funpay.sent and "на проверке" in funpay.sent[0][1]
    assert "#500" in funpay.sent[0][1]
    await db.close()


@pytest.mark.anyio
async def test_alert_only_warns_and_still_delivers(tmp_path):
    db = await new_db(tmp_path)
    pol = make_policy(duplicate_window_min=60, duplicate_action="alert", blacklist_enabled=False)
    # сначала один завершённый заказ, затем «дубль» — но действие alert ⇒ выдаём
    await db.save_order_status("499", "durov", "COMPLETED", quantity=1000, price_rub=1370.4, cost_usdt=9.1)
    result, _funpay, gameau, notifier = await run_pipeline(db, policy=pol)
    assert result["status"] == "completed"
    assert len(gameau.calls) == 1
    assert any("DUPLICATE" in text for text, _ in notifier.alerts)
    tl = await TaskTracker(db).timeline("500")
    assert any(e["event"] == "RISK_ALERT" for e in tl)
    assert (await db.get_order("500"))["status"] == "COMPLETED"
    await db.close()


@pytest.mark.anyio
async def test_ignore_policy_lets_operator_action_through(tmp_path):
    db = await new_db(tmp_path)
    await db.save_order_status("501", "durov", HOLD_STATUS, quantity=1000, price_rub=1370.4,
                               error="LARGE_ORDER: выручка больше лимита")
    pol = make_policy(max_order_revenue_rub=1)  # держит вообще всё
    result, _fp, gameau, _ = await run_pipeline(
        db,
        policy=pol,
        ignore_policy=True,
        bypass_processed_check=True,
        order={**order_payload("501"), "id": "501"},
    )
    assert result["status"] == "completed" and len(gameau.calls) == 1
    assert (await db.get_order("501"))["status"] == "COMPLETED"
    await db.close()


@pytest.mark.anyio
async def test_no_policy_keeps_old_behaviour(tmp_path):
    """policy=None — движок работает ровно как до v2.4 (совместимость)."""
    db = await new_db(tmp_path)
    result, _fp, gameau, notifier = await run_pipeline(db, policy=None, order=order_payload(price=999999))
    assert result["status"] == "completed"
    assert len(gameau.calls) == 1
    assert notifier.alerts == []
    await db.close()


@pytest.mark.anyio
async def test_release_via_retry_failed_order(tmp_path):
    """`--release`/`/release` идут через тот же пайплайн, что и ретрай."""
    db = await new_db(tmp_path)
    await db.save_order_status("600", "durov", HOLD_STATUS, quantity=1000, price_rub=1370.4,
                               error="LARGE_ORDER: big")
    gameau = RecordingGameau()
    result = await retry_failed_order(
        order_id="600",
        db=db,
        funpay_client=RecordingFunPay(),
        gameau_client=gameau,
        tg_notifier=RecordingNotifier(),
        tracker=TaskTracker(db),
        policy=make_policy(max_order_revenue_rub=1),
        ignore_policy=True,
    )
    assert result["status"] == "completed"
    assert (await db.get_order("600"))["status"] == "COMPLETED"
    tl = await TaskTracker(db).timeline("600")
    assert any(e["event"] == "RETRY_MANUAL" for e in tl)
    await db.close()


@pytest.mark.anyio
async def test_held_status_not_reported_as_stuck(tmp_path):
    """HOLD_MANUAL не «зависшая выдача»: иначе 20-минутные алерты на каждый hold."""
    db = await new_db(tmp_path)
    old = int(time.time()) - 40 * 60
    await db.save_order_status("700", "@hold", HOLD_STATUS, quantity=1000, price_rub=1370.4,
                               error="LARGE_ORDER: x")
    conn = await db.get_connection()
    await conn.execute("UPDATE orders SET created_ts = ?, updated_ts = ? WHERE order_id = '700'", (old, old))
    await conn.commit()
    assert await db.get_stuck_orders(20 * 60) == []
    assert (await db.get_orders_by_status([HOLD_STATUS]))[0]["order_id"] == "700"
    await db.close()


# ============================================================================ #
# 4. Конфигурация: откуда берутся лимиты
# ============================================================================ #


def test_config_order_policy_from_env(monkeypatch):
    for key in ("MAX_ORDER_REVENUE_RUB", "MIN_MARGIN_PCT", "DAILY_SPEND_LIMIT_USDT",
                "DUPLICATE_WINDOW_MIN", "DUPLICATE_ACTION", "BLACKLIST_ENABLED", "MUTE_HOURS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MAX_ORDER_REVENUE_RUB", "2500")
    monkeypatch.setenv("MIN_MARGIN_PCT", "15")
    monkeypatch.setenv("DAILY_SPEND_LIMIT_USDT", "300")
    monkeypatch.setenv("DUPLICATE_WINDOW_MIN", "5")
    monkeypatch.setenv("DUPLICATE_ACTION", "hold")
    monkeypatch.setenv("BLACKLIST_ENABLED", "false")
    monkeypatch.setenv("MUTE_HOURS", "23-07")
    cfg = Config.load()
    pol = cfg.order_policy()
    assert (pol.max_order_revenue_rub, pol.min_margin_pct, pol.daily_spend_limit_usdt) == (2500.0, 15.0, 300.0)
    assert (pol.duplicate_window_min, pol.duplicate_action, pol.blacklist_enabled) == (5, "hold", False)
    assert cfg.mute_window() == (23, 7)
    assert pol.rate_rub_per_usdt == cfg.active_usdt_rate


def test_mute_window_parsing_and_silence():
    assert Config(mute_hours="23:00-07:00").mute_window() == (23, 7)
    assert Config(mute_hours="0-6").mute_window() == (0, 6)
    assert Config(mute_hours="").mute_window() is None
    assert Config(mute_hours="какая-то ерунда").mute_window() is None
    assert Config(mute_hours="23-23").mute_window() is None  # нулевое окно = выкл
    assert Config(mute_hours="").is_muted() is False
    assert Config(mute_hours="0-23").is_muted(moment=time.mktime(time.struct_time((2026, 9, 18, 3, 0, 0, 1, 261, 0))))


def test_mute_window_spellings():
    """Читаем и «23-07», и «23:00 до 07:00» — владелица пишет как удобно."""
    assert Config(mute_hours="23:00 до 07:00").mute_window() == (23, 7)
    assert Config(mute_hours="7 to 1").mute_window() == (7, 1)
    assert Config(mute_hours="1.30-2").mute_window() == (1, 2)


def test_issues_flag_policy_misconfiguration():
    issues = Config(
        duplicate_action="yolo",
        mute_hours="весь день",
        max_order_revenue_rub=0,
        daily_spend_limit_usdt=0,
        min_margin_pct=95,
    ).issues()
    joined = " | ".join(issues)
    assert "DUPLICATE_ACTION" in joined
    assert "MUTE_HOURS" in joined
    assert "MAX_ORDER_REVENUE_RUB" in joined, "все лимиты выключены — надо сказать прямо"
    assert "MIN_MARGIN_PCT" in joined


def test_issues_flag_public_metrics_without_token():
    open_net = Config(metrics_enabled=True, metrics_host="0.0.0.0", metrics_token="").issues()
    assert any("METRICS_HOST" in issue for issue in open_net)
    with_token = Config(
        metrics_enabled=True, metrics_host="0.0.0.0", metrics_token="secret-token-123"
    ).issues()
    assert not any("METRICS_HOST" in issue for issue in with_token), "с токеном претензий нет"
    localhost = Config(metrics_enabled=True, metrics_host="127.0.0.1", metrics_token="").issues()
    assert not any("METRICS_HOST" in issue for issue in localhost), "localhost-open — норма"


@pytest.mark.anyio
async def test_notifier_is_muted_only_inside_window():
    from autostars.notifier.tg_alert import TelegramNotifier

    plain = TelegramNotifier(bot_token="1:token", chat_id="42")
    assert plain.is_muted() is False, "без MUTE_HOURS подавления нет"
    # окно на весь день ловится в любой час, а (23, 7) — нет (сейчас не 23:00..07:00
    # может быть только на CI с UTC; потому сравниваем поведение, а не конкретный час)
    always = TelegramNotifier(bot_token="1:token", chat_id="42", mute_window=(0, 24))
    assert always.is_muted() is True


@pytest.mark.anyio
async def test_notifier_silences_informational_alerts_in_mute_hours():
    """В тихие часы: «сделка завершена» молчит, «заказ задержан» — нет."""
    from autostars.notifier.tg_alert import TelegramNotifier

    sent: list[dict] = []

    class Response:
        status_code = 200
        text = ""

    class StubClient:
        is_closed = False

        async def post(self, url, json=None, **_kw):
            sent.append(json)
            return Response()

        async def aclose(self):
            return None

    notifier = TelegramNotifier(bot_token="1:token", chat_id="42")
    notifier._client = StubClient()
    notifier.is_muted = lambda moment=None: True  # «ночь» без зависимости от часового пояса CI

    assert await notifier.send_alert("💰 сделка", critical=False) is False
    assert sent == [], "некритичное сообщение в тихие часы отправлять нельзя"
    assert await notifier.send_alert("⛔ задержан", critical=True) is True
    assert sent and "задержан" in sent[0]["text"]
    assert await notifier.send_stats_report("статистика") is False
    assert len(sent) == 1, "плановая статистика — некритичная"
    notifier.is_muted = lambda moment=None: False
    assert await notifier.send_alert("💰 снова", critical=False) is True
    assert len(sent) == 2
    await notifier.close()


# ============================================================================ #
# 5. CLI и Telegram-команды
# ============================================================================ #


def run_cli(args, env: dict[str, str]):
    return subprocess.run(
        [sys.executable, "-m", "autostars.main", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**env},
        timeout=120,
    )


@pytest.fixture
def engine_env(isolated_env, tmp_path, monkeypatch):
    """Очищенное окружение (isolated_env) + заполненный .env с лимитами."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FUNPAY_GOLDEN_KEY=k\nGAMEAU_API_KEY=g\nMAX_ORDER_REVENUE_RUB=5000\n"
        "DAILY_SPEND_LIMIT_USDT=100\nMETRICS_TOKEN=secret\n",
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
        "AUTOSTARS_HOME": str(tmp_path),
        "AUTOSTARS_ENV": str(env_file),
        "AUTOSTARS_CONFIG": str(tmp_path / "config.json"),
        "DB_PATH": str(tmp_path / "autostars.db"),
        "LOG_FILE": str(tmp_path / "autostars.log"),
    }
    # ни одной переменной окружения из окружения разработчика/CI: иначе «у меня работало»
    for key in list(env):
        assert not key.startswith("MAX_ORDER")
    return env


@pytest.mark.anyio
async def seed_held_order(tmp_path, name: str = "autostars.db"):
    """Засев в ту же базу, которую читает CLI из фикстуры `engine_env`."""
    db = await new_db(tmp_path, name)
    await db.save_order_status("900", "@dave", HOLD_STATUS, quantity=10000, price_rub=13704.0,
                               cost_usdt=91.0, error="LARGE_ORDER: выручка 13704.00 ₽ больше лимита 5000")
    await db.add_buyer_flag("@mallory", note="не платит")
    await db.close()


@pytest.mark.anyio
async def test_cli_limits_held_blacklist_customers(engine_env, tmp_path):
    await seed_held_order(tmp_path)

    limits = run_cli(["--limits", "--json"], engine_env)
    assert limits.returncode == 0, limits.stdout + limits.stderr
    payload = json.loads(limits.stdout)
    assert payload["limits"]["max_order_revenue_rub"] == 5000.0
    assert payload["held"] == 1
    assert payload["spent_today_usdt"] == pytest.approx(91.0)
    assert payload["remaining_today_usdt"] == pytest.approx(9.0)

    human = run_cli(["--limits"], engine_env)
    assert "MAX_ORDER_REVENUE_RUB" in human.stdout and "5000 ₽" in human.stdout

    held = run_cli(["--held", "--json"], engine_env)
    assert json.loads(held.stdout)[0]["order_id"] == "900"
    assert "LARGE_ORDER" in run_cli(["--held"], engine_env).stdout

    listing = run_cli(["--blacklist", "list"], engine_env)
    assert "@mallory" in listing.stdout and "не платит" in listing.stdout

    add = run_cli(["--blacklist", "add", "@bob", "спам", "по", "поводу"], engine_env)
    assert add.returncode == 0 and "@bob" in add.stdout
    assert "спам по поводу" in run_cli(["--blacklist", "list"], engine_env).stdout
    assert run_cli(["--blacklist", "remove", "@bob"], engine_env).returncode == 0
    assert run_cli(["--blacklist", "remove", "@bob"], engine_env).returncode == 1
    assert run_cli(["--blacklist", "перевести", "@bob"], engine_env).returncode == 1

    customers = run_cli(["--customers", "--days", "7", "--json"], engine_env)
    rows = json.loads(customers.stdout)
    assert rows[0]["username"] == "@dave" and rows[0]["orders"] == 1


@pytest.mark.anyio
async def test_cli_cancel_needs_confirmation_and_closes_order(engine_env, tmp_path):
    await seed_held_order(tmp_path)

    unconfirmed = run_cli(["--cancel", "900"], engine_env)
    assert unconfirmed.returncode == 2, "отмена без -y не должна ничего менять"
    assert (await order_status(tmp_path, "900")) == HOLD_STATUS

    confirmed = run_cli(["--cancel", "900", "-y", "--note", "клиент пропал"], engine_env)
    assert confirmed.returncode == 0, confirmed.stdout
    assert "FunPay" in confirmed.stdout  # напоминаем, что сделку закрывает продавец
    assert (await order_status(tmp_path, "900")) == "CANCELLED"


async def order_status(tmp_path, order_id: str) -> str:
    db = await new_db(tmp_path, "autostars.db")
    row = await db.get_order(order_id)
    await db.close()
    return (row or {}).get("status", "")


@pytest.mark.anyio
async def test_cli_release_requires_confirmation(engine_env, tmp_path):
    await seed_held_order(tmp_path)
    rc_no = run_cli(["--release", "900"], engine_env)
    assert rc_no.returncode == 2
    assert "Подтвердите" in rc_no.stdout
    assert (await order_status(tmp_path, "900")) == HOLD_STATUS


@pytest.mark.anyio
async def test_cli_dry_run_manual_order_does_not_spend(engine_env, tmp_path):
    result = run_cli(["--order", "@someone", "3000", "--dry-run"], engine_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ручная выдача" in result.stdout
    assert "DAILY_LIMIT" in result.stdout or "политика выдачи" in result.stdout
    assert "НЕ отправлялся" in result.stdout


def test_cli_metrics_snapshot_is_prometheus_format(engine_env, tmp_path):
    out = run_cli(["--metrics"], engine_env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "autostars_daily_spend_limit_usdt 100" in out.stdout
    assert "# TYPE autostars_orders_total gauge" in out.stdout


@pytest.mark.anyio
async def test_tg_commands_limits_held_blacklist_customers(tmp_path):
    from autostars.notifier.tg_commands import build_default_handlers

    db = await new_db(tmp_path)
    await db.save_order_status("800", "@dave", HOLD_STATUS, quantity=10000, price_rub=13704.0,
                               cost_usdt=91.0, error="LARGE_ORDER: big")
    await db.add_buyer_flag("@mallory", note="не платит")
    cfg = Config(max_order_revenue_rub=5000, daily_spend_limit_usdt=100, duplicate_window_min=0,
                 blacklist_enabled=True)
    tracker = TaskTracker(db)
    handlers = build_default_handlers(cfg=cfg, db=db, gameau_client=None, notifier=None,
                                      stats=None, tracker=tracker)
    for cmd in ("limits", "held", "blacklist", "customers", "release"):
        assert cmd in handlers, f"нет обработчика /{cmd}"

    limits = await handlers["limits"]("")
    assert "5000" in limits and "91.00 USDT" in limits and "Задержано заказов: 1" in limits

    held = await handlers["held"]("")
    assert "#800" in held and "/release 800" in held

    added = await handlers["blacklist"]("add @bob за спам")
    assert "@bob" in added and "за спам" in added
    listing = await handlers["blacklist"]("")
    assert "@mallory" in listing and "@bob" in listing
    removed = await handlers["blacklist"]("remove @bob")
    assert "убран" in removed
    customers = await handlers["customers"]("7")
    assert "@dave" in customers and "зак." in customers
    usage = await handlers["release"]("")
    assert "Использование" in usage
    await db.close()


@pytest.mark.anyio
async def test_tg_release_requires_running_loop_and_held_status(tmp_path):
    from autostars.notifier.tg_commands import build_default_handlers

    db = await new_db(tmp_path)
    await db.save_order_status("801", "durov", HOLD_STATUS, quantity=1000, price_rub=1370.4)
    await db.save_order_status("802", "durov", "COMPLETED", quantity=1000, price_rub=1370.4)
    cfg = Config()
    handlers = build_default_handlers(cfg=cfg, db=db, gameau_client=None, notifier=None, stats=None,
                                      tracker=TaskTracker(db))
    # нет живого цикла → отказ (мы не покупаем «в вакууме»)
    text = await handlers["release"]("801")
    assert "работающем цикле" in text
    # заказ не в hold → подсказка про /retry
    text2 = await handlers["release"]("802")
    assert "не задержан" in text2
    text3 = await handlers["release"]("9999")
    assert "не найден" in text3
    await db.close()


def test_help_lists_new_commands():
    from autostars.notifier.tg_commands import HELP_TEXT

    for cmd in ("/limits", "/held", "/release", "/blacklist", "/customers"):
        assert cmd in HELP_TEXT


def test_retryable_statuses_include_hold():
    from autostars.database.db_manager import HOLD_STATUSES, RETRYABLE_STATUSES

    assert HOLD_STATUSES == ("HOLD_MANUAL",)
    assert "HOLD_MANUAL" in RETRYABLE_STATUSES


def test_metrics_refuse_public_bind_without_token():
    """Наружу без токена порт не поднимаем — иначе снимок читают все в сети."""
    from autostars.metrics import MetricsServer, is_local_bind

    assert is_local_bind("127.0.0.1") and is_local_bind("") and not is_local_bind("0.0.0.0")
    with pytest.raises(ValueError, match="METRICS_TOKEN"):
        MetricsServer(lambda: {}, host="0.0.0.0", port=0)
    server = MetricsServer(lambda: {}, host="0.0.0.0", port=0, token="abc")
    assert server.port == 0, "с токеном — можно"
