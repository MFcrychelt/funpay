"""Тесты v2.2: отзывчивость и полнота функционала.

- Чат-мониторинг WAITING_USERNAME (подхват @username из чата, без спама)
- Пауза/резюм, ретрай проваленных заказов, контроль баланса GAMEAU
- Интерактивный TG-сервер команд (разрешение, dispatch, long-poll цикл)
- Персистентные HTTP-соединения GAMEAU
- Защита от повторной обработки заказов (любые статусы в БД)
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autostars.database.db_manager import DBManager  # noqa: E402
from autostars.clients.gameau import GameauClient  # noqa: E402
from autostars.notifier.tg_alert import TelegramNotifier  # noqa: E402
from autostars.notifier.tg_commands import TelegramCommandServer  # noqa: E402
from autostars.services import bot_control  # noqa: E402
from autostars.services.task_tracker import TaskTracker  # noqa: E402
from autostars.services.order_processor import process_paid_order  # noqa: E402

RATE_2 = 87.63


# ============================================================================ #
# Хелперы
# ============================================================================ #

async def make_db(tmp_path, name: str = "v22.db") -> DBManager:
    db = DBManager(str(tmp_path / name))
    await db.init_db()
    return db


def minutes_ago(m: float) -> int:
    return int(time.time()) - int(m * 60)


class MockFunPayClient:
    def __init__(self, last_message: str | None = None):
        self.sent = []
        self.last_message = last_message

    async def send_message(self, node, text):
        self.sent.append((node, text))
        return True

    async def get_last_buyer_message(self, node):
        return self.last_message


class MockGameau:
    def __init__(self, mode: str = "success"):
        self.mode = mode
        self.calls = []

    async def buy_telegram_stars(self, username, quantity, order_id,
                                 max_charge_usdt=9.50, hide_sender=False):
        self.calls.append({"username": username, "quantity": quantity,
                           "order_id": order_id, "maxCharge": max_charge_usdt})
        idem = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))
        if self.mode == "success":
            return {"success": True,
                    "data": {"orderId": f"G-{order_id}", "chargedAmount": 9.10},
                    "idempotency_key": idem}
        return {"success": False, "error": "NETWORK_TIMEOUT"}

    async def find_stars_package(self, stars):
        return None


class MockNotifier:
    def __init__(self):
        self.alerts = []
        self.current_rate = RATE_2

    def calculate_profit(self, order_price_rub, usdt_cost, rate=None, tron_energy_rub=None):
        r = rate or self.current_rate
        return round(order_price_rub - (usdt_cost * r), 2)

    async def send_alert(self, text, parse_mode="HTML"):
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


# ============================================================================ #
# 1. Защита от повторной обработки и спам-спа
# ============================================================================ #

@pytest.mark.anyio
async def test_is_order_processed_any_known_status(tmp_path):
    """Заказ с ЛЮБЫМ заведённым статусом не уходит в пайплайн повторно
    (в т.ч. FAILED — раньше он обрабатывался заново каждый полл)."""
    db = await make_db(tmp_path)
    await db.save_order_status("X-1", "durov", "FAILED", error="NETWORK_TIMEOUT")
    await db.save_order_status("X-2", None, "WAITING_USERNAME")
    assert await db.is_order_processed("X-1") is True
    assert await db.is_order_processed("X-2") is True
    assert await db.is_order_processed("X-NEW") is False
    await db.close()


@pytest.mark.anyio
async def test_waiting_username_no_spam(tmp_path):
    """Заказ без ника: запрос отправляется ОДИН раз, повторные прогоны
    очереди не спамят покупателя."""
    db = await make_db(tmp_path)
    fp, gameau, tg = MockFunPayClient(), MockGameau(), MockNotifier()

    order = {"id": "SP-1", "chat_node": "c1", "last_message": "привет, оплатил",
             "price": 1370.40}
    r1 = await process_paid_order(order, fp, gameau, db, tg)
    r2 = await process_paid_order(order, fp, gameau, db, tg)

    assert r1["status"] == "waiting_username"
    assert r2["status"] == "already_processed"
    assert len(fp.sent) == 1  # ровно один запрос ника
    assert len(gameau.calls) == 0
    await db.close()


# ============================================================================ #
# 2. Чат-мониторинг: покупатель ответил ником в чате
# ============================================================================ #

@pytest.mark.anyio
async def test_chat_monitor_picks_up_username(tmp_path):
    """WAITING_USERNAME + покупатель написал «@real_user 500 звёзд» в чате
    → авто-выдача запускается с корректным количеством."""
    db = await make_db(tmp_path)
    now = int(time.time())
    conn = await db.get_connection()
    await conn.execute(
        "INSERT INTO orders (order_id, username, chat_node, quantity, price_rub, status,"
        " created_ts, updated_ts, created_at, updated_at)"
        " VALUES ('CM-1', NULL, 'chat_cm', 1000, 685.20, 'WAITING_USERNAME', ?, ?,"
        " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (now - 600, now - 600),
    )
    await conn.commit()

    fp = MockFunPayClient(last_message="вот мой ник @real_user и 500 звёзд")
    gameau, tg = MockGameau(), MockNotifier()
    tracker = TaskTracker(db)

    started = await bot_control.process_waiting_username_orders(
        db=db, funpay_client=fp, gameau_client=gameau, tg_notifier=tg,
        tracker=tracker, default_quantity=1000,
    )

    assert started == 1
    assert len(gameau.calls) == 1
    assert gameau.calls[0]["username"] == "real_user"
    assert gameau.calls[0]["quantity"] == 500  # из сообщения, а не дефолт 1000
    row = await db.get_order("CM-1")
    assert row["status"] == "COMPLETED"
    tl = await tracker.timeline("CM-1")
    assert any(e["event"] == "CHAT_USERNAME_RECEIVED" for e in tl)
    await db.close()


@pytest.mark.anyio
async def test_chat_monitor_no_reply_no_action(tmp_path):
    """Покупатель ещё не ответил → ничего не делаем, без ошибок."""
    db = await make_db(tmp_path)
    conn = await db.get_connection()
    now = int(time.time())
    await conn.execute(
        "INSERT INTO orders (order_id, username, chat_node, quantity, price_rub, status,"
        " created_ts, updated_ts, created_at, updated_at)"
        " VALUES ('CM-2', NULL, 'chat_cm2', 1000, 1370.40, 'WAITING_USERNAME', ?, ?,"
        " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (now - 600, now - 600),
    )
    await conn.commit()

    fp = MockFunPayClient(last_message=None)  # ответа нет
    gameau, tg = MockGameau(), MockNotifier()

    started = await bot_control.process_waiting_username_orders(
        db=db, funpay_client=fp, gameau_client=gameau, tg_notifier=tg,
        tracker=TaskTracker(db), default_quantity=1000,
    )
    assert started == 0
    assert len(gameau.calls) == 0
    row = await db.get_order("CM-2")
    assert row["status"] == "WAITING_USERNAME"
    await db.close()


# ============================================================================ #
# 3. Пауза / резюм
# ============================================================================ #

@pytest.mark.anyio
async def test_pause_resume_persisted(tmp_path):
    db = await make_db(tmp_path)
    assert await bot_control.is_bot_paused(db) is False
    await bot_control.set_bot_paused(db, True)
    assert await bot_control.is_bot_paused(db) is True
    await bot_control.set_bot_paused(db, False)
    assert await bot_control.is_bot_paused(db) is False
    await db.close()


# ============================================================================ #
# 4. Ретрай проваленных заказов
# ============================================================================ #

@pytest.mark.anyio
async def test_retry_failed_order_success(tmp_path):
    db = await make_db(tmp_path)
    conn = await db.get_connection()
    now = int(time.time())
    await conn.execute(
        "INSERT INTO orders (order_id, username, chat_node, quantity, price_rub, status,"
        " error, created_ts, updated_ts, created_at, updated_at)"
        " VALUES ('RT-1', 'durov', 'chat_rt', 1000, 1370.40, 'FAILED_LOW_BALANCE',"
        " 'LOW_BALANCE', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (now - 1800, now - 1800),
    )
    await conn.commit()

    fp, gameau, tg = MockFunPayClient(), MockGameau("success"), MockNotifier()
    result = await bot_control.retry_failed_order(
        order_id="RT-1", db=db, funpay_client=fp, gameau_client=gameau,
        tg_notifier=tg, tracker=TaskTracker(db), default_quantity=1000,
    )

    assert result["status"] == "completed"
    assert len(gameau.calls) == 1
    row = await db.get_order("RT-1")
    assert row["status"] == "COMPLETED"
    tl = await TaskTracker(db).timeline("RT-1")
    assert any(e["event"] == "RETRY_MANUAL" for e in tl)
    await db.close()


@pytest.mark.anyio
async def test_retry_completed_rejected(tmp_path):
    db = await make_db(tmp_path)
    await db.save_order_status("RT-2", "durov", "COMPLETED")
    result = await bot_control.retry_failed_order(
        order_id="RT-2", db=db, funpay_client=MockFunPayClient(),
        gameau_client=MockGameau(), tg_notifier=MockNotifier(),
        tracker=TaskTracker(db),
    )
    assert result["status"] == "not_retryable"
    assert result["current_status"] == "COMPLETED"
    await db.close()


@pytest.mark.anyio
async def test_retry_not_found(tmp_path):
    db = await make_db(tmp_path)
    result = await bot_control.retry_failed_order(
        order_id="NO-SUCH", db=db, funpay_client=MockFunPayClient(),
        gameau_client=MockGameau(), tg_notifier=MockNotifier(),
        tracker=TaskTracker(db),
    )
    assert result["status"] == "not_found"
    await db.close()


# ============================================================================ #
# 5. Контроль баланса GAMEAU
# ============================================================================ #

class BalanceGameau:
    def __init__(self, response):
        self.response = response

    async def get_account(self):
        return self.response


@pytest.mark.anyio
async def test_balance_low_triggers_alert(tmp_path):
    notifier = MockNotifier()
    gameau = BalanceGameau({"balance": 10.5, "currency": "USD", "plan": "Standard"})
    info = await bot_control.check_gameau_balance(gameau, notifier, threshold_usdt=50.0)
    assert info is not None and info["balance"] == 10.5
    assert len(notifier.alerts) == 1
    assert "БАЛАНС GAMEAU НИЗКИЙ" in notifier.alerts[0]


@pytest.mark.anyio
async def test_balance_ok_no_alert(tmp_path):
    notifier = MockNotifier()
    gameau = BalanceGameau({"balance": 500.0, "currency": "USD"})
    info = await bot_control.check_gameau_balance(gameau, notifier, threshold_usdt=50.0)
    assert info["balance"] == 500.0
    assert len(notifier.alerts) == 0


@pytest.mark.anyio
async def test_balance_error_no_crash(tmp_path):
    notifier = MockNotifier()
    gameau = BalanceGameau({"error": "HTTP_401"})
    info = await bot_control.check_gameau_balance(gameau, notifier, threshold_usdt=50.0)
    assert info is None
    assert len(notifier.alerts) == 0


# ============================================================================ #
# 6. Интерактивный TG-сервер команд
# ============================================================================ #

class FakeTgNotifier:
    def __init__(self, chat_id="42"):
        self.bot_token = "FAKE_TOKEN"
        self.chat_id = str(chat_id)
        self.sent = []

    @property
    def is_configured(self):
        return True

    async def send_stats_report(self, text, parse_mode="HTML"):
        self.sent.append(text)
        return True

    def get_profit_summary(self, price, cost):
        return {
            "variant_1": {"cost_rub": cost * 110.0, "profit_rub": price - cost * 110.0},
            "variant_2": {"cost_rub": cost * RATE_2, "profit_rub": price - cost * RATE_2},
        }


def make_server(chat_id="42", stats_text="STATS-BODY"):
    notifier = FakeTgNotifier(chat_id)

    async def h_stats(_a):
        return stats_text

    async def h_help(_a):
        return "HELP-BODY"

    server = TelegramCommandServer(
        notifier, {"stats": h_stats, "help": h_help}
    )
    return server, notifier


@pytest.mark.anyio
async def test_tg_dispatch_known_and_unknown():
    server, _ = make_server()
    assert await server.dispatch("/stats") == "STATS-BODY"
    assert await server.dispatch("/stats extra args") == "STATS-BODY"
    assert await server.dispatch("/help") == "HELP-BODY"
    unknown = await server.dispatch("/nope")
    assert "Неизвестная команда" in unknown and "/help" in unknown
    # Нераспознанное (не команда) — молчим
    assert await server.dispatch("просто текст") is None
    assert await server.dispatch("") is None
    await server.close()


@pytest.mark.anyio
async def test_tg_command_parsing():
    server, _ = make_server()
    assert server.parse_command("/stats  extra") == ("stats", "extra")
    assert server.parse_command("/stats@MyBotName x") == ("stats", "x")
    assert server.parse_command("/balance") == ("balance", "")
    assert server.parse_command("hello") == ("", "hello")
    await server.close()


@pytest.mark.anyio
async def test_tg_authorization_only_owner():
    server, _ = make_server(chat_id="42")
    assert server._authorized(42) is True
    assert server._authorized("42") is True
    assert server._authorized(99) is False
    assert server._authorized("stranger") is False
    await server.close()


@pytest.mark.anyio
async def test_tg_server_run_loop_end_to_end():
    """Long-poll цикл: команда владельца → ответ; чужой чат → игнор;
    остановка по stop_event."""
    server, notifier = make_server(chat_id="42")

    updates_batch = [
        {"update_id": 1, "message": {"text": "/stats", "chat": {"id": 42}}},
        {"update_id": 2, "message": {"text": "/stats", "chat": {"id": 99}}},  # не владелец
        {"update_id": 3, "message": {"text": "привет", "chat": {"id": 42}}},  # не команда
    ]
    calls = {"n": 0}
    stop = asyncio.Event()

    async def fake_get_updates(offset):
        calls["n"] += 1
        if calls["n"] == 1:
            return updates_batch
        stop.set()  # после первой партии — останавливаем цикл
        return []

    server._get_updates = fake_get_updates
    await server.run(stop)

    assert calls["n"] >= 2
    # Ответ ровно один — только на команду владельца
    assert notifier.sent == ["STATS-BODY"]


@pytest.mark.anyio
async def test_tg_server_disabled_without_config():
    class Unconfigured:
        bot_token = ""
        chat_id = ""

        @property
        def is_configured(self):
            return False

    server = TelegramCommandServer(Unconfigured(), {})
    assert server.enabled is False
    stop = asyncio.Event()
    stop.set()
    await server.run(stop)  # не должно впадать в цикл
    await server.close()


# ============================================================================ #
# 7. Персистентные HTTP-соединения GAMEAU
# ============================================================================ #

@pytest.mark.anyio
async def test_gameau_persistent_client():
    client = GameauClient("K", base_url="https://gameau.us/api/v1")
    http1 = await client._http()
    http2 = await client._http()
    assert http1 is http2  # переиспользование соединения
    await client.close()
    assert client._client is None
    http3 = await client._http()
    assert http3 is not http1  # после close — новое соединение
    await client.close()


# ============================================================================ #
# 8. Разбивка длинных TG-сообщений
# ============================================================================ #

@pytest.mark.anyio
async def test_stats_report_split_long_message():
    notifier = TelegramNotifier(bot_token="T", chat_id="1")
    captured = []

    async def fake_send(text, parse_mode="HTML"):
        captured.append(text)
        return True

    notifier.send_alert = fake_send
    long_text = "\n".join(f"строка {i} " + "x" * 200 for i in range(30))  # ~6.6K
    await notifier.send_stats_report(long_text)
    assert len(captured) >= 2  # разбито на части
    assert all(len(c) <= 4096 for c in captured)
    await notifier.close()
