"""Интеграционный тест пайплайна автовыдачи на моках (без сети)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Config
from bot.delivery import DeliveryEngine
from bot.gameau import GameauError
from bot.state import State


class FakeMessage:
    def __init__(self, text: str, author_id: int):
        self.text = text
        self.author_id = author_id
        self.id = 1


class FakeFunPay:
    def __init__(self, seller_id: int = 999):
        self.account = SimpleNamespace(id=seller_id, username="seller")
        self.seller_id = seller_id
        self.chat_messages: list[FakeMessage] = []
        self.sent: list[tuple[str, str]] = []   # (order_id, text)
        self.refunds: list[str] = []

    def ensure_session(self) -> None:
        pass

    def order_messages(self, order) -> list[FakeMessage]:
        return list(self.chat_messages)

    def send_message(self, order, text: str) -> bool:
        self.sent.append((str(order.id), text))
        return True

    def refund(self, order) -> bool:
        self.refunds.append(str(order.id))
        return True


class FakeGameau:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.created: list[dict] = []
        self.catalog = [
            {"name": "Telegram Stars 100", "price": 1.7, "orderable": True,
             "order": {"body": {"quantity": 100}}},
            {"name": "Telegram Stars 250", "price": 4.1, "orderable": True,
             "order": {"body": {"quantity": 250}}},
        ]
        self._catalog_cache = (time.time(), self.catalog)

    def find_stars_package(self, stars: int):
        exact = [i for i in self.catalog if i["order"]["body"]["quantity"] == stars]
        if exact:
            return exact[0]
        greater = [i for i in self.catalog if i["order"]["body"]["quantity"] >= stars]
        return min(greater, key=lambda i: i["order"]["body"]["quantity"]) if greater else None

    @staticmethod
    def package_stars(item: dict) -> int:
        return item["order"]["body"]["quantity"]

    def send_stars(self, username: str, quantity: int, max_charge: float,
                   idempotency_key: str | None = None,
                   hide_sender: bool = False):
        self.created.append({
            "username": username, "quantity": quantity,
            "max_charge": max_charge, "idem": idempotency_key,
        })
        if self.fail:
            raise GameauError("имитация сбоя")
        return {"id": "ord-1", "status": "processing"}

    def wait_for_completion(self, order_id: str, *, poll_interval: float = 0.01,
                            timeout: float = 1.0):
        return {"id": order_id, "status": "success"}

    def get_account(self):
        return {"username": "test", "balance": 100.0, "currency": "USD"}


def make_engine(tmp_path: Path, funpay: FakeFunPay, gameau: FakeGameau,
                **cfg_overrides) -> DeliveryEngine:
    cfg = Config()
    cfg.poll_interval = 0
    cfg.reminder_interval = 0.0
    cfg.username_wait_timeout = 10**9
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    state = State(tmp_path / "state.json")
    return DeliveryEngine(cfg, funpay, gameau, state)  # type: ignore[arg-type]


def order(order_id="ABC1", description="100 звёзд", amount=1, buyer="buyer1"):
    return SimpleNamespace(
        id=order_id, description=description, amount=amount,
        price=150.0, buyer_id=42, buyer_username=buyer,
    )


# --------------------------------------------------------------------------- #
# Сценарий 1: юзернейм есть в описании заказа — выдача сразу.
# --------------------------------------------------------------------------- #
def test_delivery_with_username_in_description(tmp_path: Path) -> None:
    funpay = FakeFunPay()
    gameau = FakeGameau()
    engine = make_engine(tmp_path, funpay, gameau)

    engine.process_order(order(description="100 звёзд @real_user"))

    assert len(gameau.created) == 1
    call = gameau.created[0]
    assert call["username"] == "real_user"
    assert call["quantity"] == 100
    assert call["max_charge"] == pytest.approx(1.7 * 1.5)
    # покупателю отправлено подтверждение
    assert funpay.sent and "real_user" in funpay.sent[0][1]
    # состояние зафиксировано
    rec = engine.state.get_order("ABC1")
    assert rec["status"] == "done"
    assert rec["idempotency_key"] == call["idem"]


# --------------------------------------------------------------------------- #
# Сценарий 2: юзернейма нет — бот просит его в чате и ждёт.
# --------------------------------------------------------------------------- #
def test_missing_username_asks_in_chat(tmp_path: Path) -> None:
    funpay = FakeFunPay()
    gameau = FakeGameau()
    engine = make_engine(tmp_path, funpay, gameau)

    engine.process_order(order(description="100 звёзд"))

    assert gameau.created == []                      # звёзды не отправлялись
    assert funpay.sent and "юзернейм" in funpay.sent[0][1]
    rec = engine.state.get_order("ABC1")
    assert rec["status"] == "waiting"
    assert rec.get("asked_username_at")


# --------------------------------------------------------------------------- #
# Сценарий 3: юзернейм пришёл в чате после просьбы — выдача на следующем круге.
# --------------------------------------------------------------------------- #
def test_username_arrived_in_chat(tmp_path: Path) -> None:
    funpay = FakeFunPay()
    gameau = FakeGameau()
    engine = make_engine(tmp_path, funpay, gameau)

    o = order(description="250 звёзд")
    engine.process_order(o)
    assert funpay.sent and "юзернейм" in funpay.sent[0][1]

    # Покупатель отвечает в чате.
    funpay.chat_messages.append(FakeMessage("тг: my_telegram", author_id=42))
    engine.process_order(o)

    assert len(gameau.created) == 1
    assert gameau.created[0]["username"] == "my_telegram"
    assert gameau.created[0]["quantity"] == 250
    assert engine.state.get_order("ABC1")["status"] == "done"


# --------------------------------------------------------------------------- #
# Сценарий 4: повторная обработка выданного заказа не списывает звёзды повторно.
# --------------------------------------------------------------------------- #
def test_no_double_delivery(tmp_path: Path) -> None:
    funpay = FakeFunPay()
    gameau = FakeGameau()
    engine = make_engine(tmp_path, funpay, gameau)

    o = order(description="100 звёзд @real_user")
    engine.process_order(o)
    engine.process_order(o)
    engine.process_order(o)

    assert len(gameau.created) == 1


# --------------------------------------------------------------------------- #
# Сценарий 5: мультипокупка — количество умножается на amount, а при отсутствии
# точного пакета округляется до ближайшего большего пакета в каталоге.
# --------------------------------------------------------------------------- #
def test_multibuy_amount(tmp_path: Path) -> None:
    funpay = FakeFunPay()
    gameau = FakeGameau()
    engine = make_engine(tmp_path, funpay, gameau)

    engine.process_order(order(description="100 звёзд ×2 @bulk_buyer", amount=2))
    # нужно 200 звёзд; точного пакета нет → ближайший больший = 250
    assert len(gameau.created) == 1
    assert gameau.created[0]["quantity"] == 250
    assert engine.state.get_order("ABC1")["stars"] == 250


# --------------------------------------------------------------------------- #
# Сценарий 6: сбой GAMEAU — после исчерпания попыток статус failed
# и сообщение покупателю об ошибке.
# --------------------------------------------------------------------------- #
def test_gameau_failure(tmp_path: Path) -> None:
    funpay = FakeFunPay()
    gameau = FakeGameau(fail=True)
    engine = make_engine(tmp_path, funpay, gameau, max_create_attempts=3)

    o = order(description="100 звёзд @real_user")
    engine.process_order(o)  # попытка 1 — повтор
    assert engine.state.get_order("ABC1")["status"] == "new"
    engine.process_order(o)  # попытка 2 — повтор
    engine.process_order(o)  # попытка 3 — фатально

    rec = engine.state.get_order("ABC1")
    assert rec["status"] == "failed"
    assert funpay.refunds == []  # refund_on_failure по умолчанию выключен
    assert any("ошибка" in text.lower() for _, text in funpay.sent)


# --------------------------------------------------------------------------- #
# Сценарий 7: неизвестный лот (не удалось определить количество звёзд).
# --------------------------------------------------------------------------- #
def test_unknown_lot(tmp_path: Path) -> None:
    funpay = FakeFunPay()
    gameau = FakeGameau()
    engine = make_engine(tmp_path, funpay, gameau)

    engine.process_order(order(description="Аккаунт GTA 5"))

    assert gameau.created == []
    rec = engine.state.get_order("ABC1")
    assert rec["status"] == "no_rule"


# --------------------------------------------------------------------------- #
# Сценарий 8: правило лота из конфига имеет приоритет.
# --------------------------------------------------------------------------- #
def test_lot_rule_priority(tmp_path: Path) -> None:
    funpay = FakeFunPay()
    gameau = FakeGameau()
    engine = make_engine(
        tmp_path, funpay, gameau,
        lot_rules=[{"pattern": r"премиум пакет", "stars": 250}],
    )

    engine.process_order(order(description="Премиум пакет @user_one"))
    assert gameau.created[0]["quantity"] == 250


# --------------------------------------------------------------------------- #
# Сценарий 9: тайм-аут ожидания статуса — заказ остаётся в ожидании
# и дожидается на следующем круге (без повторного списания).
# --------------------------------------------------------------------------- #
class TimeoutOnceGameau(FakeGameau):
    """Первый вызов ожидания — тайм-аут, второй — успех."""

    def __init__(self):
        super().__init__()
        self.waits = 0

    def wait_for_completion(self, order_id, *, poll_interval=0.01, timeout=1.0):
        self.waits += 1
        if self.waits == 1:
            from bot.gameau import GameauTimeoutError
            raise GameauTimeoutError("имитация тайм-аута")
        return {"id": order_id, "status": "success"}


def test_timeout_then_resume(tmp_path: Path) -> None:
    funpay = FakeFunPay()
    gameau = TimeoutOnceGameau()
    engine = make_engine(tmp_path, funpay, gameau)

    o = order(description="100 звёзд @real_user")
    engine.process_order(o)  # первый круг: тайм-аут → остаёмся в ожидании
    rec = engine.state.get_order("ABC1")
    assert rec["status"] == "sending"
    assert rec["gameau_order_id"] == "ord-1"

    engine.process_order(o)  # второй круг: статус получен, выдача завершена
    assert engine.state.get_order("ABC1")["status"] == "done"
    # заказ в GAMEAU создавался ровно один раз — повторного списания нет
    assert len(gameau.created) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
