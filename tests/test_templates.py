"""Шаблоны ответов покупателю: текст — часть продукта, а не часть кода.

Проверяем три вещи, которые легко сломать:
  1. значения по умолчанию = ровно те тексты, что движок писал до появления
     шаблонов (апгрейд не должен менять переписку с покупателями);
  2. `.env` умеет многострочные значения в двойных кавычках (`\\n` разворачивается),
     а неизвестный плейсхолдер не роняет выдачу;
  3. `process_paid_order` берёт текст из шаблона: пустая строка = «не отправляем»,
     свои плейсхолдеры подставляются, ошибочный шаблон не ломает заказ.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autostars.config import Config, parse_env_text
from autostars.database.db_manager import DBManager
from autostars.services.order_processor import process_paid_order
from autostars.services.policy import OrderPolicy, ReplyTemplates
from autostars.services.task_tracker import TaskTracker

ROOT = Path(__file__).resolve().parents[1]

# ============================================================================ #
# 1. Рендеринг
# ============================================================================ #


def test_defaults_match_previous_hardcoded_texts():
    """Тот самый текст, что был в коде до v2.4 — байт в байт."""
    templates = ReplyTemplates()
    assert templates.need_username == (
        "Здравствуйте! Не удалось автоматически распознать ваш Telegram @username. "
        "Пожалуйста, напишите его ответным сообщением в формате: @username"
    )
    rendered = templates.render(
        "delivered", username="durov", quantity=1000, order_id="123", price_rub=1370.40
    )
    assert rendered == (
        "✅ Здравствуйте! 1 000 Telegram Stars успешно зачислены на аккаунт @durov.\n\n"
        "Пожалуйста, проверьте баланс в Telegram и подтвердите успешное выполнение заказа на FunPay! "
        "Спасибо за покупку!"
    )


def test_empty_template_renders_empty_and_is_documented_as_silence():
    assert ReplyTemplates(hold="").render("hold", order_id="1") == ""
    assert ReplyTemplates(error="   ").render("error") == ""
    assert ReplyTemplates().hold == "" and ReplyTemplates().error == ""


def test_unknown_placeholder_survives_and_stays_visible():
    """Опечатка в {плейсхолдере} не должна ронять выдачу заказа."""
    text = ReplyTemplates(delivered="Привет, {username}! Заказ {order_id}, лот {lot_id}").render(
        "delivered", username="bob", order_id="42"
    )
    assert text == "Привет, bob! Заказ 42, лот {lot_id}"


def test_format_braces_in_text_are_not_interpreted():
    """Текст без фигурных скобок проходит как есть; {quantity_spaces} форматируется."""
    templates = ReplyTemplates(delivered="{quantity} ⭐ = {quantity_spaces} на счёт {username}")
    out = templates.render("delivered", username="nik", quantity=12500)
    assert out == "12500 ⭐ = 12 500 на счёт nik"


def test_render_of_unknown_kind_is_empty():
    assert ReplyTemplates().render("no_such_template", username="x") == ""


def test_from_config_prefers_configured_text():
    cfg = Config(reply_delivered="Готово: {quantity}⭐ для @{username}")
    templates = ReplyTemplates.from_config(cfg)
    # {username} — ник без '@' (так его хранит БД), знак ставит сам шаблон
    assert templates.render("delivered", username="durov", quantity=500) == "Готово: 500⭐ для @durov"
    # пустые поля в конфиге = «не отправляем», а не «верни дефолт»
    quiet = Config(reply_hold="", reply_error="")
    assert ReplyTemplates.from_config(quiet).hold == ""


# ============================================================================ #
# 2. .env: многострочные значения
# ============================================================================ #


def test_env_multiline_templates_roundtrip(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        'REPLY_DELIVERED="Первая строка.\\n\\nВторая строка: {username}."\n'
        "REPLY_HOLD='одна строка'\n"
        "REPLY_ERROR=текст без кавычек\n"
        "MAX_ORDER_REVENUE_RUB=2500  # комментарий не попадёт в значение\n",
        encoding="utf-8",
    )
    values = parse_env_text(env.read_text(encoding="utf-8"))
    assert values["REPLY_DELIVERED"] == "Первая строка.\n\nВторая строка: {username}."
    assert values["REPLY_HOLD"] == "одна строка"
    assert values["REPLY_ERROR"] == "текст без кавычек"
    assert values["MAX_ORDER_REVENUE_RUB"] == "2500"


def test_env_with_quotes_does_not_break_other_keys(tmp_path, monkeypatch):
    """Кавычки в шаблоне не должны «съедать» соседние строки файла."""
    env = tmp_path / ".env"
    env.write_text(
        "FUNPAY_GOLDEN_KEY=k\n"
        'REPLY_DELIVERED="Привет! Скажите «да» на FunPay."\n'
        "GAMEAU_API_KEY=g\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOSTARS_ENV", str(env))
    cfg = Config.load()
    assert cfg.gameau_key == "g"
    assert "FunPay" in cfg.reply_delivered


def test_documented_templates_in_env_example_match_defaults():
    """`.env.example` обязан предлагать те же тексты, что и движок (иначе «пример врёт»)."""
    example = ROOT / ".env.example"
    values = parse_env_text(example.read_text(encoding="utf-8"))
    defaults = ReplyTemplates()
    assert values["REPLY_NEED_USERNAME"] == defaults.need_username
    assert values["REPLY_DELIVERED"] == defaults.delivered
    assert values["REPLY_HOLD"] == "" and values["REPLY_ERROR"] == ""


def test_config_templates_and_policy_are_consistent():
    cfg = Config(reply_error="Ой: {reason}", max_order_revenue_rub=100, rate_variant_2=87.63)
    pol = cfg.order_policy()
    assert pol.templates.error == "Ой: {reason}"
    assert pol.max_order_revenue_rub == 100.0
    assert pol.rate_rub_per_usdt == 87.63


# ============================================================================ #
# 3. Пайплайн использует шаблоны
# ============================================================================ #


class RecordingGameau:
    def __init__(self):
        self.calls: list[dict] = []

    async def find_stars_package(self, quantity: int):
        return None

    async def buy_telegram_stars(self, username, quantity, order_id, **_kw):
        self.calls.append({"username": username, "quantity": quantity})
        return {"success": True, "data": {"orderId": f"G-{order_id}", "status": "completed",
                                          "chargedAmount": 9.1}}

    async def get_order(self, gameau_order_id: str):
        return {"success": True, "data": {"orderId": gameau_order_id, "status": "completed"}}


class RecordingFunPay:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, node, text, hide_sender=False):
        self.sent.append((str(node), text))
        return True


class SilentNotifier:
    current_rate = 87.63

    def __init__(self):
        self.alerts: list[str] = []

    def calculate_profit(self, order_price_rub, usdt_cost, rate=None, tron_energy_rub=None):
        return round(order_price_rub - usdt_cost * 87.63, 2)

    async def send_alert(self, text, parse_mode="HTML", *, critical=True):
        self.alerts.append(text)
        return True

    async def alert_order_completed(self, order_id, username, **_kw):
        return True

    async def alert_low_balance(self, order_id):
        return True

    async def alert_price_exceeded(self, order_id, max_charge):
        return True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def run(db, *, message, templates, order_id="1000", quantity=None):
    funpay, gameau, notifier = RecordingFunPay(), RecordingGameau(), SilentNotifier()
    result = await process_paid_order(
        order_data={
            "id": order_id,
            "chat_node": f"chat-{order_id}",
            "last_message": message,
            "description": message,
            "quantity": quantity,
        },
        funpay_client=funpay,
        gameau_client=gameau,
        db=db,
        tg_notifier=notifier,
        task_tracker=TaskTracker(db),
        policy=OrderPolicy(templates=templates),
        usdt_per_1000_stars=9.10,
        whitebird_rate=87.63,
    )
    return result, funpay, gameau, notifier


async def make_db(tmp_path):
    db = DBManager(str(tmp_path / "tpl.db"))
    await db.init_db()
    return db


@pytest.mark.anyio
async def test_username_request_uses_template(tmp_path):
    db = await make_db(tmp_path)
    result, funpay, gameau, _ = await run(
        db, message="хочу 1000 звёзд, вот мой ник", templates=ReplyTemplates(need_username="Напишите @ник, пожалуйста")
    )
    assert result["status"] == "waiting_username"
    assert funpay.sent == [("chat-1000", "Напишите @ник, пожалуйста")]
    assert gameau.calls == []
    await db.close()


@pytest.mark.anyio
async def test_empty_need_username_sends_nothing(tmp_path):
    db = await make_db(tmp_path)
    result, funpay, _g, _ = await run(db, message="привет", templates=ReplyTemplates(need_username=""))
    assert result["status"] == "waiting_username"
    assert funpay.sent == []
    await db.close()


@pytest.mark.anyio
async def test_delivered_template_reaches_buyer(tmp_path):
    db = await make_db(tmp_path)
    result, funpay, gameau, _ = await run(
        db,
        message="@durov 2000",
        quantity=2000,
        templates=ReplyTemplates(delivered="{quantity_spaces} звёзд на @{username}, заказ #{order_id}"),
    )
    assert result["status"] == "completed"
    assert gameau.calls and funpay.sent
    assert funpay.sent[0][1] == "2 000 звёзд на @durov, заказ #1000"
    await db.close()


@pytest.mark.anyio
async def test_default_delivered_text_unchanged_for_buyers(tmp_path):
    """Ничего не настроено → покупатель читает тот же текст, что и раньше."""
    db = await make_db(tmp_path)
    result, funpay, _g, _ = await run(db, message="@durov 1000", templates=ReplyTemplates())
    assert result["status"] == "completed"
    assert funpay.sent[0][1].startswith("✅ Здравствуйте! 1 000 Telegram Stars успешно зачислены")
    await db.close()


@pytest.mark.anyio
async def test_delivery_failure_template_sent_once(tmp_path):
    class FailingGameau(RecordingGameau):
        async def buy_telegram_stars(self, username, quantity, order_id, **_kw):
            self.calls.append({"username": username})
            return {"success": False, "error": "GAMEAU_DELIVERY_FAILED"}

    db = await make_db(tmp_path)
    funpay, notifier = RecordingFunPay(), SilentNotifier()
    gameau = FailingGameau()
    result = await process_paid_order(
        order_data={"id": "1001", "chat_node": "chat-1001", "last_message": "@durov 1000",
                    "description": "@durov 1000"},
        funpay_client=funpay,
        gameau_client=gameau,
        db=db,
        tg_notifier=notifier,
        task_tracker=TaskTracker(db),
        policy=OrderPolicy(templates=ReplyTemplates(error="Не получилось: {reason}. Вернём деньги через FunPay.")),
        usdt_per_1000_stars=9.10,
        whitebird_rate=87.63,
    )
    assert result["status"] == "failed"
    assert any("Не получилось" in text for _node, text in funpay.sent)
    assert (await db.get_order("1001"))["status"].startswith("FAILED")
    await db.close()


@pytest.mark.anyio
async def test_delivery_failure_without_template_is_quiet(tmp_path):
    class FailingGameau(RecordingGameau):
        async def buy_telegram_stars(self, username, quantity, order_id, **_kw):
            return {"success": False, "error": "GAMEAU_DELIVERY_FAILED"}

    db = await make_db(tmp_path)
    funpay = RecordingFunPay()
    await process_paid_order(
        order_data={"id": "1002", "chat_node": "chat-1002", "last_message": "@durov 1000",
                    "description": "@durov 1000"},
        funpay_client=funpay,
        gameau_client=FailingGameau(),
        db=db,
        tg_notifier=SilentNotifier(),
        task_tracker=TaskTracker(db),
        policy=OrderPolicy(templates=ReplyTemplates()),
        usdt_per_1000_stars=9.10,
        whitebird_rate=87.63,
    )
    assert funpay.sent == [], "пустой REPLY_ERROR = молчим (как и раньше)"
    await db.close()


# ============================================================================ #
# 4. CLI --templates и GUI-редактор
# ============================================================================ #


def test_cli_templates_lists_placeholders_and_values(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FUNPAY_GOLDEN_KEY=k\nGAMEAU_API_KEY=g\nREPLY_HOLD=\"Ждите ответа продавца\"\n", encoding="utf-8")
    monkeypatch.setenv("AUTOSTARS_ENV", str(env))
    monkeypatch.setenv("AUTOSTARS_HOME", str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "autostars.main", "--templates"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "AUTOSTARS_ENV": str(env),
            "AUTOSTARS_HOME": str(tmp_path),
            "AUTOSTARS_CONFIG": str(tmp_path / "config.json"),
            "DB_PATH": str(tmp_path / "t.db"),
            "LOG_FILE": str(tmp_path / "t.log"),
        },
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REPLY_HOLD" in result.stdout and "Ждите ответа продавца" in result.stdout
    assert "{quantity_spaces}" in result.stdout and "REPLY_DELIVERED" in result.stdout


def test_gui_roundtrip_multiline_template(tmp_path, monkeypatch):
    """Многострочный шаблон из GUI должен вернуться в движок без потерь."""
    pytest.importorskip("flet")
    from autostars.gui import settings

    monkeypatch.setenv("AUTOSTARS_HOME", str(tmp_path))
    monkeypatch.setenv("AUTOSTARS_ENV", str(tmp_path / ".env"))
    # apply_updates пишет значения ещё и в os.environ (чтобы GUI-процесс видел
    # новые настройки) — чистим, иначе тест зависит от порядка запуска
    for key in ("REPLY_DELIVERED", "REPLY_HOLD", "REPLY_ERROR", "REPLY_NEED_USERNAME"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("FUNPAY_GOLDEN_KEY=k\nGAMEAU_API_KEY=g\n", encoding="utf-8")

    field = next(f for f in settings.FIELDS if f.key == "REPLY_DELIVERED")
    # ключа в файле нет → форма показывает текст движка, а не пустоту
    current = settings.read_settings()
    assert settings.display_value(field, current) == ReplyTemplates().delivered

    new_text = "Звёзды у вас ✅\n\n{quantity_spaces} шт. на @{username}."
    updates = settings.plan_updates({"REPLY_DELIVERED": new_text}, current)
    assert updates == {"REPLY_DELIVERED": new_text}
    path, changed = settings.apply_updates(updates)
    assert changed == 1
    raw = path.read_text(encoding="utf-8")
    assert "\n" not in raw.split("REPLY_DELIVERED=")[1].splitlines()[0].strip('"') or "\\n" in raw
    assert parse_env_text(raw)["REPLY_DELIVERED"] == new_text
    assert Config.load().reply_delivered == new_text
    # повторное сохранение «как есть» ничего не меняет
    assert settings.plan_updates({"REPLY_DELIVERED": new_text}, settings.read_settings()) == {}


def test_gui_rejects_unknown_placeholder_in_template():
    pytest.importorskip("flet")
    from autostars.gui.settings import validate

    errors = validate({"REPLY_HOLD": "Ждите {order_is}"})
    assert errors and "неизвестная подстановка" in errors[0]
    assert validate({"REPLY_HOLD": "Ждите {order_id}"}) == []
