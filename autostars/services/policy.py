"""Политика выдачи: лимиты сделки, шаблоны ответов покупателю, стоп-лист.

Почему это отдельный слой: движок умел только «купить и выдать», а реальная
торговля — это ещё и «не покупать». Списанные USDT обратно не вернуть, поэтому
проверки идут ДО запроса в GAMEAU, а их вывод — одно из трёх решений:

  ``ignore``  — живём как раньше (проверка выключена);
  ``alert``   — покупаем, но предупреждаем владельца (полезно поначалу);
  ``hold``    — заказ уходит в ``HOLD_MANUAL`` и ждёт человека
                (``--release <ID>`` / TG ``/release <ID>``).

Шаблоны ответов живут здесь же: тон текста — то, что видят покупатели, и менять его
нужно в конфиге, а не в коде. Значения по умолчанию совпадают с текстами, которые
движок писал раньше, поэтому «включил шаблоны — ничего не поменялось».
"""

from __future__ import annotations

import contextlib
import dataclasses
import time
from collections.abc import Iterable
from typing import Any

ACTION_IGNORE = "ignore"
ACTION_ALERT = "alert"
ACTION_HOLD = "hold"

ACTIONS = (ACTION_IGNORE, ACTION_ALERT, ACTION_HOLD)

#: Статус заказа, который никто не выдаёт без человека.
HOLD_STATUS = "HOLD_MANUAL"


def format_username(value: object) -> str:
    """Ник для сообщений: в БД движок хранит username без '@', печатаем с '@'."""
    text = str(value or "").strip().lstrip("@")
    return f"@{text}" if text else "?"


# --------------------------------------------------------------------------- #
# Шаблоны
# --------------------------------------------------------------------------- #


class _SafeDict(dict[str, Any]):
    """Неизвестный ключ не роняет ответ покупателю — оставляем {placeholder} как есть."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclasses.dataclass(frozen=True)
class ReplyTemplates:
    """Тексты в чат FunPay. Пустая строка = «ничего не отправляем»."""

    need_username: str = (
        "Здравствуйте! Не удалось автоматически распознать ваш Telegram @username. "
        "Пожалуйста, напишите его ответным сообщением в формате: @username"
    )
    delivered: str = (
        "✅ Здравствуйте! {quantity_spaces} Telegram Stars успешно зачислены на аккаунт @{username}.\n\n"
        "Пожалуйста, проверьте баланс в Telegram и подтвердите успешное выполнение заказа на FunPay! "
        "Спасибо за покупку!"
    )
    hold: str = ""
    error: str = ""

    @classmethod
    def from_config(cls, cfg: Any) -> ReplyTemplates:
        """Собирает шаблоны из Config (пустое поле → дефолт этого класса)."""
        return cls(
            need_username=str(getattr(cfg, "reply_need_username", cls.need_username) or cls.need_username),
            delivered=str(getattr(cfg, "reply_delivered", cls.delivered) or cls.delivered),
            hold=str(getattr(cfg, "reply_hold", cls.hold) or ""),
            error=str(getattr(cfg, "reply_error", cls.error) or ""),
        )

    def render(self, kind: str, **context: Any) -> str:
        template = str(getattr(self, kind, "") or "")
        if not template:
            return ""
        values: dict[str, Any] = dict(context)
        stars = values.get("quantity")
        if isinstance(stars, (int, float)) and "quantity_spaces" not in values:
            values["quantity_spaces"] = f"{int(stars):,}".replace(",", " ")
        return template.format_map(_SafeDict(**values)).strip()


# --------------------------------------------------------------------------- #
# Лимиты
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class OrderPolicy:
    """Настройки риск-проверок (см. ``Config.order_policy``)."""

    max_order_revenue_rub: float = 0.0
    min_margin_pct: float = 0.0
    daily_spend_limit_usdt: float = 0.0
    duplicate_window_min: int = 0
    duplicate_action: str = ACTION_ALERT
    blacklist_enabled: bool = True
    rate_rub_per_usdt: float = 87.63
    templates: ReplyTemplates = dataclasses.field(default_factory=ReplyTemplates)

    @property
    def any_check_on(self) -> bool:
        return bool(
            self.max_order_revenue_rub > 0
            or self.min_margin_pct > 0
            or self.daily_spend_limit_usdt > 0
            or self.duplicate_window_min > 0
            or self.blacklist_enabled
        )

    def day_start_ts(self, now: float | None = None) -> int:
        """Полночь локального времени — граница «суточного» бюджета."""
        current = time.localtime(now if now is not None else time.time())
        return int(time.mktime((current.tm_year, current.tm_mon, current.tm_mday, 0, 0, 0, 0, 0, -1)))


@dataclasses.dataclass(frozen=True)
class RiskDecision:
    """Одна сработавшая проверка."""

    code: str
    action: str
    reason: str

    @property
    def is_hold(self) -> bool:
        return self.action == ACTION_HOLD

    @property
    def is_alert(self) -> bool:
        return self.action in (ACTION_ALERT, ACTION_HOLD)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "action": self.action, "reason": self.reason}


def evaluate_limits(
    policy: OrderPolicy,
    *,
    price_rub: float,
    cost_rub: float,
    spent_today_usdt: float = 0.0,
    order_cost_usdt: float = 0.0,
) -> list[RiskDecision]:
    """Локальные проверки (без БД): крупная сделка, маржа, суточный бюджет.

    Отдельная функция — потому что её надо уметь пересчитать для `--limits`
    и в тестах, без подключения к базе и к GAMEAU.
    """
    checks: list[RiskDecision] = []

    if policy.max_order_revenue_rub > 0 and price_rub > policy.max_order_revenue_rub:
        checks.append(
            RiskDecision(
                code="LARGE_ORDER",
                action=ACTION_HOLD,
                reason=(
                    f"выручка {price_rub:.2f} ₽ больше лимита сделки "
                    f"{policy.max_order_revenue_rub:.0f} ₽"
                ),
            )
        )

    if policy.min_margin_pct > 0 and price_rub > 0:
        margin_pct = (price_rub - cost_rub) / price_rub * 100.0
        if margin_pct < policy.min_margin_pct:
            checks.append(
                RiskDecision(
                    code="LOW_MARGIN",
                    action=ACTION_HOLD,
                    reason=(
                        f"маржа {margin_pct:.1f}% ниже порога {policy.min_margin_pct:.1f}% "
                        f"(выручка {price_rub:.2f} ₽, затраты ~{cost_rub:.2f} ₽)"
                    ),
                )
            )

    if policy.daily_spend_limit_usdt > 0:
        projected = spent_today_usdt + order_cost_usdt
        if projected > policy.daily_spend_limit_usdt:
            checks.append(
                RiskDecision(
                    code="DAILY_LIMIT",
                    action=ACTION_HOLD,
                    reason=(
                        f"суточный бюджет USDT: уже {spent_today_usdt:.2f} + ещё {order_cost_usdt:.2f} = "
                        f"{projected:.2f} > лимита {policy.daily_spend_limit_usdt:.2f}"
                    ),
                )
            )

    return checks


def action_for_duplicates(policy: OrderPolicy) -> str:
    action = (policy.duplicate_action or ACTION_ALERT).strip().lower()
    return action if action in ACTIONS else ACTION_ALERT


def summarize(checks: Iterable[RiskDecision]) -> tuple[RiskDecision | None, list[RiskDecision]]:
    """Возвращает (чем-то, что блокирует выдачу, список всех предупреждений)."""
    items = [c for c in checks if c.action != ACTION_IGNORE]
    hold = next((c for c in items if c.is_hold), None)
    return hold, items


async def evaluate_order_risk(
    policy: OrderPolicy,
    db: Any,
    *,
    order_id: str,
    username: str,
    quantity: int,
    price_rub: float,
    cost_rub: float,
    order_cost_usdt: float,
    now: float | None = None,
) -> list[RiskDecision]:
    """Полный набор проверок: лимиты + дубликаты + стоп-лист."""
    now_ts = now if now is not None else time.time()
    checks = evaluate_limits(
        policy,
        price_rub=price_rub,
        cost_rub=cost_rub,
        spent_today_usdt=await _spent_today(db, policy, now_ts),
        order_cost_usdt=order_cost_usdt,
    )

    if policy.blacklist_enabled and db is not None and hasattr(db, "is_buyer_flagged"):
        with contextlib.suppress(Exception):
            if await db.is_buyer_flagged(username, "blacklist"):
                checks.append(
                    RiskDecision(
                        code="BLACKLISTED",
                        action=ACTION_HOLD,
                        reason=f"покупатель {format_username(username)} в стоп-листе",
                    )
                )

    window = int(policy.duplicate_window_min or 0)
    if window > 0 and db is not None and hasattr(db, "find_recent_order_by_username"):
        since = int(now_ts) - window * 60
        duplicate = None
        with contextlib.suppress(Exception):
            duplicate = await db.find_recent_order_by_username(
                username, quantity, since_ts=since, exclude_order_id=order_id
            )
        if duplicate:
            checks.append(
                RiskDecision(
                    code="DUPLICATE",
                    action=action_for_duplicates(policy),
                    reason=(
                        f"тот же ник и {quantity}⭐ уже обрабатывались {window} мин назад "
                        f"(заказ #{duplicate.get('order_id')}, статус {duplicate.get('status')})"
                    ),
                )
            )

    return checks


async def _spent_today(db: Any, policy: OrderPolicy, now_ts: float) -> float:
    if db is None or not hasattr(db, "sum_cost_usdt_since"):
        return 0.0
    with contextlib.suppress(Exception):
        return float(await db.sum_cost_usdt_since(policy.day_start_ts(now_ts)))
    return 0.0


def alert_text(
    order_id: str,
    username: str,
    quantity: int,
    price_rub: float,
    checks: list[RiskDecision],
    *,
    held: bool,
) -> str:
    """Сообщение владельцу: что за заказы и почему (hold — критично, alert — нет)."""
    head = "⛔ ЗАДЕРЖАН ЗАКАЗ" if held else "⚠️ Проверка политики выдачи"
    lines = [
        f"<b>{head}:</b> #{order_id} {format_username(username)} • {quantity}⭐ • {price_rub:.2f} ₽",
    ]
    for check in checks:
        lines.append(f"• <b>{check.code}</b> ({check.action}): {check.reason}")
    if held:
        lines.append(
            "Не выдан. Продолжить: <code>--release "
            f"{order_id}</code> (CLI) или <code>/release {order_id}</code> (Telegram), "
            "отменить: <code>--cancel " + str(order_id) + "</code>"
        )
    return "\n".join(lines)
