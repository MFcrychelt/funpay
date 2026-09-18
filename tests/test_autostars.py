"""Комплексные тесты для AutoStars (FunPay <-> Gameau Engine).

Покрывают:
1. Базу данных SQLite и таблицы orders, idempotency_logs, idempotency_keys (Шаг 2)
2. Long Polling runner/ и автоматическое обновление csrf_token при 403 Forbidden (Шаг 3)
3. Модуль парсинга Telegram Username на реальных сообщениях (Шаг 4)
4. Интеграцию с Telegram Alert и калькуляцию прибыли Whitebird 87.63 RUB (Шаг 5)
5. GameauClient с Idempotency-Key, maxCharge и ретраями
6. Полный пайплайн order_processor
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autostars.clients.funpay import FunPayClient
from autostars.clients.gameau import GameauClient
from autostars.database.db_manager import DBManager
from autostars.notifier.tg_alert import TelegramNotifier
from autostars.services.order_processor import process_paid_order
from autostars.services.parser import extract_stars_quantity, extract_telegram_username


# ============================================================================ #
# 1. ТЕСТЫ БАЗЫ ДАННЫХ (Шаг 2)
# ============================================================================ #
@pytest.mark.anyio
async def test_database_initialization_and_crud(tmp_path):
    db_file = str(tmp_path / "test_autostars.db")
    db = DBManager(db_file)
    await db.init_db()

    # Проверка исходного состояния
    assert await db.is_order_processed("ORD-101") is False
    assert await db.get_order("ORD-101") is None

    # Создание заказа со статусом PROCESSING
    await db.save_order_status("ORD-101", "durov", "PROCESSING", chat_node="12345", quantity=1000)
    assert await db.is_order_processed("ORD-101") is True  # PROCESSING считается in-flight

    order = await db.get_order("ORD-101")
    assert order is not None
    assert order["order_id"] == "ORD-101"
    assert order["username"] == "durov"
    assert order["status"] == "PROCESSING"

    # Завершение заказа со статусом COMPLETED и финансовыми метриками
    await db.save_order_status(
        "ORD-101",
        "durov",
        "COMPLETED",
        price_rub=1370.40,
        cost_usdt=9.10,
        profit_rub=572.97,
    )
    assert await db.is_order_processed("ORD-101") is True
    updated = await db.get_order("ORD-101")
    assert updated["status"] == "COMPLETED"
    assert updated["profit_rub"] == pytest.approx(572.97)

    # Проверка логирования идемпотентности
    idem_key = str(uuid.uuid4())
    await db.log_idempotency(idem_key, "ORD-101", "payload_req", "payload_resp", "SUCCESS")
    idem_record = await db.get_idempotency(idem_key)
    assert idem_record is not None
    assert idem_record["order_id"] == "ORD-101"
    assert idem_record["status"] == "SUCCESS"

    await db.close()


# ============================================================================ #
# 2. ТЕСТЫ ПАРСЕРА TELEGRAM HANDLES (Шаг 4)
# ============================================================================ #
@pytest.mark.parametrize(
    "message,expected",
    [
        # Прямой @username
        ("@vampire_achao", "vampire_achao"),
        ("мой ник @ivan_petrov, кидай туда", "ivan_petrov"),
        ("Здравствуйте! Выдайте на @alex_2024 пожалуйста", "alex_2024"),
        ("⭐ заказ для @user_12345 ⭐", "user_12345"),
        ("@nick_1", "nick_1"),
        # Ссылки t.me и telegram.me
        ("t.me/durov", "durov"),
        ("https://t.me/cool_seller", "cool_seller"),
        ("http://telegram.me/tg_buyer_777", "tg_buyer_777"),
        ("вот ссылка t.me/super_star_99 жду звёзды", "super_star_99"),
        # Ключевые слова
        ("тг: my_telegram_nick", "my_telegram_nick"),
        ("телеграм: seller_pro", "seller_pro"),
        ("телега - alex_prime", "alex_prime"),
        ("юзернейм: cool_guy", "cool_guy"),
        ("tg: crypto_whale", "crypto_whale"),
        # Одиночный ник
        ("vampireachao", "vampireachao"),
        ("just_nickname_99", "just_nickname_99"),
        # Невалидные сообщения и стоп-слова
        ("привет", None),
        ("спасибо за заказ", None),
        ("telegram", None),
        ("funpay", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_telegram_username(message, expected):
    assert extract_telegram_username(message) == expected


@pytest.mark.parametrize(
    "text,expected_stars",
    [
        ("1000 звёзд Telegram", 1000),
        ("500 stars", 500),
        ("⭐ 250", 250),
        ("Telegram Stars 100", 100),
        ("50 звёздочек", 50),
        ("просто текст без звёзд", None),
    ],
)
def test_extract_stars_quantity(text, expected_stars):
    assert extract_stars_quantity(text) == expected_stars


# ============================================================================ #
# 3. ТЕСТЫ ФИНАНСОВОЙ МОДЕЛИ И TELEGRAM ALERT (Шаг 5)
# ============================================================================ #
def test_whitebird_profit_calculation():
    notifier = TelegramNotifier(rate_variant_1=110.0, rate_variant_2=87.63, active_variant=2)

    # 1000 Stars = 9.10 USDT в Gameau API
    # Себестоимость Вариант 2 (Whitebird): 9.10 * 87.63 = 797.433 RUB
    # Выручка на FunPay: 1370.40 RUB
    # Чистая прибыль: 1370.40 - 797.433 = 572.967 -> 572.97 RUB!
    profit_v2 = notifier.calculate_profit(order_price_rub=1370.40, usdt_cost=9.10, rate=87.63)
    assert profit_v2 == 572.97

    # Себестоимость Вариант 1 (Анонимный обмен): 9.10 * 110.0 = 1001.00 RUB
    # Чистая прибыль: 1370.40 - 1001.00 = 369.40 RUB
    profit_v1 = notifier.calculate_profit(order_price_rub=1370.40, usdt_cost=9.10, rate=110.00)
    assert profit_v1 == 369.40

    # Сравнение через get_profit_summary
    summary = notifier.get_profit_summary(order_price_rub=1370.40, usdt_cost=9.10)
    assert summary["variant_1"]["profit_rub"] == 369.40
    assert summary["variant_2"]["profit_rub"] == 572.97
    assert summary["active_profit_rub"] == 572.97


@pytest.mark.anyio
async def test_gameau_catalog_parsing_and_find_package():
    fake_catalog = {
        "ok": True,
        "catalog": {
            "items": [
                {"name": "Telegram Stars 100", "price": 0.95, "orderable": True, "order": {"body": {"quantity": 100}}},
                {"name": "Telegram Stars 250", "price": 2.30, "orderable": True, "order": {"body": {"quantity": 250}}},
                {"name": "Telegram Stars 500", "price": 4.55, "orderable": True, "order": {"body": {"quantity": 500}}},
                {"name": "Telegram Stars 1000", "price": 9.10, "orderable": True, "order": {"body": {"quantity": 1000}}},
            ]
        }
    }

    def mock_catalog_transport(request: httpx.Request):
        if "catalog" in request.url.path:
            return httpx.Response(200, json=fake_catalog)
        return httpx.Response(404)

    client = GameauClient("TEST_KEY", base_url="https://gameau.us/api/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_catalog_transport)) as ac:
        # Патчим вызов httpx в клиенте

        async def mocked_get_catalog(**kwargs):
            resp = await ac.get("https://gameau.us/api/v1/catalog?type=telegramStars")
            data = resp.json()
            return data["catalog"]["items"]

        client.get_catalog = mocked_get_catalog

        # 1. Поиск точного пакета 1000 звёзд
        pkg_1000 = await client.find_stars_package(1000)
        assert pkg_1000 is not None
        assert client.extract_item_stars(pkg_1000) == 1000
        assert pkg_1000["price"] == 9.10

        # 2. Поиск пакета на 200 звёзд (точного нет -> берется 250)
        pkg_200 = await client.find_stars_package(200)
        assert pkg_200 is not None
        assert client.extract_item_stars(pkg_200) == 250

        # 3. Извлечение количества звёзд
        assert client.extract_item_stars({"name": "Telegram Stars 500"}) == 500


@pytest.mark.anyio
async def test_telegram_alert_sending():
    sent_requests = []

    def mock_transport(request: httpx.Request):
        sent_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        bot_token="TEST_BOT_TOKEN",
        chat_id="12345678",
        whitebird_rate=87.63,
    )
    # Тестируем alert_order_completed с кастомным клиентом
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_transport)) as client:
        # Патчим метод send_alert для перехвата запросов

        async def mocked_send(text, parse_mode="HTML", *, critical=True):
            payload = {"chat_id": notifier.chat_id, "text": text, "parse_mode": parse_mode}
            resp = await client.post("https://api.telegram.org/botTEST_BOT_TOKEN/sendMessage", json=payload)
            return resp.status_code == 200

        notifier.send_alert = mocked_send

        success = await notifier.alert_order_completed("ORD-777", "durov", 572.97)
        assert success is True
        assert len(sent_requests) == 1
        data = json.loads(sent_requests[0].content)
        assert "572.97 RUB" in data["text"]
        assert "@durov" in data["text"]

        await notifier.alert_low_balance("ORD-888")
        assert len(sent_requests) == 2
        data2 = json.loads(sent_requests[1].content)
        assert "НИЗКИЙ БАЛАНС GAMEAU" in data2["text"]
        assert "Whitebird" in data2["text"]


# ============================================================================ #
# 4. ТЕСТЫ GAMEAU CLIENT (Idempotency Key, maxCharge, обработка статусов)
# ============================================================================ #
@pytest.mark.anyio
async def test_gameau_client_success():
    sent_headers = {}
    sent_body = {}

    def mock_transport(request: httpx.Request):
        nonlocal sent_headers, sent_body
        sent_headers = dict(request.headers)
        sent_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"orderId": "G-12345", "status": "completed", "charge": 9.10},
        )

    client = GameauClient("TEST_API_KEY")
    # Переопределяем AsyncClient через MockTransport
    async def mock_async_client(*args, **kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(mock_transport), **kwargs)

    # Тестируем вызов buy_telegram_stars
    order_id = "FP-12345"
    expected_idem = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))

    # Создаем вызов с замоканным транспортом
    transport = httpx.MockTransport(mock_transport)
    async with httpx.AsyncClient(transport=transport) as ac:
        req_headers = {**client.headers, "Idempotency-Key": expected_idem}
        resp = await ac.post(
            f"{client.base_url}/telegramStars",
            json={"username": "durov", "quantity": 1000, "maxCharge": 9.50},
            headers=req_headers,
        )
        assert resp.status_code == 200
        assert sent_headers["idempotency-key"] == expected_idem
        assert sent_body["username"] == "durov"
        assert sent_body["quantity"] == 1000
        assert sent_body["maxCharge"] == 9.50


@pytest.mark.anyio
async def test_gameau_client_price_exceeded_and_low_balance():
    # 1. 400 with maxCharge error
    def transport_price_exceeded(request: httpx.Request):
        return httpx.Response(400, text="Price exceeds maxCharge limit")

    GameauClient("TEST_API_KEY")
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_price_exceeded)) as ac:
        resp = await ac.post("https://gameau.us/api/telegramStars", json={})
        assert resp.status_code == 400 and "maxCharge" in resp.text

    # 2. 402 Low balance
    def transport_low_balance(request: httpx.Request):
        return httpx.Response(402, json={"error": "Insufficient balance"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_low_balance)) as ac:
        resp = await ac.post("https://gameau.us/api/telegramStars", json={})
        assert resp.status_code == 402


# ============================================================================ #
# 5. ТЕСТЫ FUNPAY LONG POLLING И CSRF RECOVERY (Шаг 3)
# ============================================================================ #
@pytest.mark.anyio
async def test_funpay_csrf_extraction_and_403_recovery():
    html_page = """
    <html>
      <body data-app-data='{"userId": 98765, "csrf-token": "new_secret_csrf_12345"}'>
        <div class="user-link-name">StarSeller</div>
      </body>
    </html>
    """

    call_count = 0

    def mock_transport(request: httpx.Request):
        nonlocal call_count
        call_count += 1
        path = request.url.path

        if path in ("/orders/trade", "/"):
            return httpx.Response(200, text=html_page)

        if path == "/runner/":
            # При первом запросе имитируем 403 Forbidden (устаревший CSRF)
            if call_count == 2:
                return httpx.Response(403, text="Forbidden / CSRF Expired")
            # После обновления CSRF возвращаем 200 OK
            return httpx.Response(
                200,
                json={
                    "objects": [
                        {"type": "orders_counters", "tag": "tag_orders_99"},
                        {"type": "chat_bookmarks", "tag": "tag_chats_99"},
                    ]
                },
            )

        return httpx.Response(404)

    client = FunPayClient("test_golden_key")
    # Подменяем внутренний httpx клиент на MockTransport
    client._client = httpx.AsyncClient(
        base_url="https://funpay.com",
        transport=httpx.MockTransport(mock_transport),
    )

    # 1. Первичное получение CSRF
    csrf = await client.refresh_csrf()
    assert csrf == "new_secret_csrf_12345"
    assert client.user_id == 98765
    assert client.username == "StarSeller"

    # 2. Опрос runner/ — первый запрос получает 403, автообновляет CSRF и успешно выполняет второй запрос
    poll_result = await client.poll_runner()
    assert poll_result is not None
    assert client.orders_tag == "tag_orders_99"
    assert client.chats_tag == "tag_chats_99"

    await client.close()


# ============================================================================ #
# 6. ИНТЕГРАЦИОННЫЙ ТЕСТ ORDER_PROCESSOR (Шаг 1 - Шаг 6)
# ============================================================================ #
class MockFunPayClient:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, node, text):
        self.sent_messages.append((node, text))
        return True

    async def get_last_buyer_message(self, node):
        return None


class MockGameauClient:
    def __init__(self, mode="success"):
        self.mode = mode
        self.calls = []

    async def buy_telegram_stars(self, username, quantity, order_id, max_charge_usdt=9.50, hide_sender=False):
        self.calls.append({
            "username": username,
            "quantity": quantity,
            "order_id": order_id,
            "maxCharge": max_charge_usdt,
        })
        idem = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))

        if self.mode == "success":
            return {
                "success": True,
                "data": {"orderId": "G-999", "charge": 9.10},
                "idempotency_key": idem,
            }
        elif self.mode == "low_balance":
            return {"success": False, "error": "LOW_BALANCE"}
        elif self.mode == "price_exceeded":
            return {"success": False, "error": "PRICE_EXCEEDED"}
        else:
            return {"success": False, "error": "INTERNAL_ERROR"}


class MockNotifier:
    def __init__(self):
        self.alerts = []
        self.whitebird_rate = 87.63
        self.rate_variant_1 = 110.00
        self.rate_variant_2 = 87.63

    def calculate_profit(self, order_price_rub, usdt_cost, rate=None, tron_energy_rub=None):
        r = rate or self.whitebird_rate
        return round(order_price_rub - (usdt_cost * r), 2)

    async def send_alert(self, text, parse_mode="HTML", *, critical=True):
        self.alerts.append(text)
        return True

    async def alert_order_completed(self, order_id, username, profit_rub=572.97, order_price_rub=None, usdt_cost=9.10):
        text = f"💰 Заказ #{order_id} выполнен! +{profit_rub:.2f} RUB чистой прибыли (Отправлено @{username})"
        self.alerts.append(text)
        return True

    async def alert_low_balance(self, order_id):
        text = f"🚨 <b>ОШИБКА: НИЗКИЙ БАЛАНС GAMEAU!</b> Заказ #{order_id} остановлен. Срочно пополните USDT через Whitebird!"
        self.alerts.append(text)
        return True


@pytest.mark.anyio
async def test_order_processor_success_flow(tmp_path):
    db_file = str(tmp_path / "orders_test.db")
    db = DBManager(db_file)
    await db.init_db()

    fp = MockFunPayClient()
    gameau = MockGameauClient(mode="success")
    tg = MockNotifier()

    order_data = {
        "id": "FP-1001",
        "chat_node": "chat_555",
        "last_message": "Здравствуйте! Мой тг @durov, спасибо!",
        "price": 1370.40,
        "quantity": 1000,
    }

    res = await process_paid_order(order_data, fp, gameau, db, tg)

    assert res["status"] == "completed"
    assert res["profit_rub"] == 572.97

    # Проверка вызова Gameau
    assert len(gameau.calls) == 1
    assert gameau.calls[0]["username"] == "durov"
    assert gameau.calls[0]["quantity"] == 1000

    # Проверка ответа в чат FunPay
    assert len(fp.sent_messages) == 1
    node, reply_text = fp.sent_messages[0]
    assert node == "chat_555"
    assert "1 000 Telegram Stars успешно зачислены на аккаунт @durov" in reply_text

    # Проверка алерта в Telegram
    assert len(tg.alerts) == 1
    assert "+572.97 RUB чистой прибыли" in tg.alerts[0]
    assert "@durov" in tg.alerts[0]

    # Проверка статуса в БД
    saved_order = await db.get_order("FP-1001")
    assert saved_order["status"] == "COMPLETED"
    assert saved_order["profit_rub"] == pytest.approx(572.97)

    # Повторная обработка того же заказа не приводит к двойной покупке
    res_repeat = await process_paid_order(order_data, fp, gameau, db, tg)
    assert res_repeat["status"] == "already_processed"
    assert len(gameau.calls) == 1  # Больше вызовов Gameau не было

    await db.close()


@pytest.mark.anyio
async def test_order_processor_missing_username_clarification(tmp_path):
    db_file = str(tmp_path / "orders_no_user.db")
    db = DBManager(db_file)
    await db.init_db()

    fp = MockFunPayClient()
    gameau = MockGameauClient()
    tg = MockNotifier()

    order_data = {
        "id": "FP-1002",
        "chat_node": "chat_666",
        "last_message": "Привет, я оплатил заказ",  # нет юзернейма
        "price": 1370.40,
    }

    res = await process_paid_order(order_data, fp, gameau, db, tg)
    assert res["status"] == "waiting_username"

    # Gameau не вызывался
    assert len(gameau.calls) == 0

    # Покупателю отправлен запрос юзернейма
    assert len(fp.sent_messages) == 1
    assert "Не удалось автоматически распознать ваш Telegram @username" in fp.sent_messages[0][1]

    # Статус в БД
    rec = await db.get_order("FP-1002")
    assert rec["status"] == "WAITING_USERNAME"

    await db.close()


@pytest.mark.anyio
async def test_order_processor_low_balance_alert(tmp_path):
    db_file = str(tmp_path / "orders_low_balance.db")
    db = DBManager(db_file)
    await db.init_db()

    fp = MockFunPayClient()
    gameau = MockGameauClient(mode="low_balance")
    tg = MockNotifier()

    order_data = {
        "id": "FP-1003",
        "chat_node": "chat_777",
        "last_message": "@durov",
    }

    res = await process_paid_order(order_data, fp, gameau, db, tg)
    assert res["status"] == "failed"
    assert res["error"] == "LOW_BALANCE"

    # Алерт владельцу о пополнении через Whitebird
    assert len(tg.alerts) == 1
    assert "НИЗКИЙ БАЛАНС GAMEAU" in tg.alerts[0]
    assert "Whitebird" in tg.alerts[0]

    # Статус в БД
    rec = await db.get_order("FP-1003")
    assert rec["status"] == "FAILED_LOW_BALANCE"

    await db.close()
