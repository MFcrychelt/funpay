"""Статистика выполнения задач и финансовая отчётность.

Окна: 1 час, 2 часа, 3 часа, 4 часа, 6 часов, 24 часа (день),
календарный день (с 00:00 локального времени) и всего.

По каждому окну считается:
- заказы: всего / выполнено / провалено / в работе / ожидание ника
- звёзды выдано (по завершённым)
- выручка (RUB), затраты (USDT и RUB по активному курсу), прибыль (RUB)
- убытки: сделки в минус (маржа < 0) и потерянная выручка по проваленным заказам
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("autostars.stats")

# Часовые окна (названия) — по ТЗ: час / 2 / 3 / 4 / 6 / день
HOUR_WINDOWS: tuple[tuple[str, int], ...] = (
    ("1 час", 1),
    ("2 часа", 2),
    ("3 часа", 3),
    ("4 часа", 4),
    ("6 часов", 6),
    ("24 часа", 24),
)

TERMINAL_FAIL_STATUSES = (
    "FAILED",
    "FAILED_LOW_BALANCE",
    "FAILED_PRICE_EXCEEDED",
    "FAILED_DELIVERY",
    "CANCELLED",
)


@dataclass
class WindowStats:
    """Агрегированная статистика за одно временное окно."""

    label: str
    from_ts: int
    to_ts: int
    total: int = 0
    completed: int = 0
    failed: int = 0
    in_progress: int = 0
    waiting_username: int = 0
    #: задержано политикой выдачи и ждёт решения человека
    held: int = 0
    stars: int = 0
    revenue_rub: float = 0.0
    cost_usdt: float = 0.0
    cost_rub: float = 0.0
    profit_rub: float = 0.0
    loss_orders: int = 0
    loss_rub: float = 0.0
    failed_potential_rub: float = 0.0
    avg_profit_rub: float = 0.0
    best_profit_rub: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def margin_pct(self) -> float:
        if self.revenue_rub <= 0:
            return 0.0
        return round(self.profit_rub / self.revenue_rub * 100, 1)

    @property
    def is_empty(self) -> bool:
        return self.total == 0

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable вид (для GUI, `--json` и внешних дашбордов)."""
        return {
            "label": self.label,
            "from_ts": self.from_ts,
            "to_ts": self.to_ts,
            "orders_total": self.total,
            "orders_completed": self.completed,
            "orders_failed": self.failed,
            "orders_in_progress": self.in_progress,
            "orders_waiting_username": self.waiting_username,
            "orders_held": self.held,
            "stars": self.stars,
            "revenue_rub": round(self.revenue_rub, 2),
            "cost_usdt": round(self.cost_usdt, 4),
            "cost_rub": round(self.cost_rub, 2),
            "profit_rub": round(self.profit_rub, 2),
            "margin_pct": self.margin_pct,
            "avg_profit_rub": round(self.avg_profit_rub, 2),
            "best_profit_rub": round(self.best_profit_rub, 2),
            "loss_orders": self.loss_orders,
            "loss_rub": round(self.loss_rub, 2),
            "failed_potential_rub": round(self.failed_potential_rub, 2),
        }


class StatisticsService:
    """Считает и оформляет статистику по окнам 1ч/2ч/3ч/4ч/6ч/день."""

    def __init__(
        self,
        db: Any,
        rate_variant_1: float = 110.00,
        rate_variant_2: float = 87.63,
        active_variant: int = 2,
        tron_energy_fee_rub: float = 0.0,
    ):
        self.db = db
        self.rate_variant_1 = float(rate_variant_1)
        self.rate_variant_2 = float(rate_variant_2)
        self.active_variant = int(active_variant)
        self.tron_energy_fee_rub = float(tron_energy_fee_rub)

    @property
    def active_rate(self) -> float:
        return self.rate_variant_1 if self.active_variant == 1 else self.rate_variant_2

    # ------------------------------------------------------------------ #
    # Вычисление
    # ------------------------------------------------------------------ #

    def _apply_finance(self, raw: dict[str, Any], label: str, from_ts: int, to_ts: int) -> WindowStats:
        """Дополняет сырые SQL-агрегаты: себестоимость в RUB по активному курсу."""
        cost_rub = float(raw.get("cost_rub") or 0.0)
        # Для старых записей cost_rub мог не заполняться — пересчитываем по курсу
        if cost_rub <= 0 and raw.get("cost_usdt"):
            cost_rub = raw["cost_usdt"] * self.active_rate
        if raw.get("completed") and self.tron_energy_fee_rub:
            cost_rub += raw["completed"] * self.tron_energy_fee_rub

        return WindowStats(
            label=label,
            from_ts=from_ts,
            to_ts=to_ts,
            total=int(raw.get("total") or 0),
            completed=int(raw.get("completed") or 0),
            failed=int(raw.get("failed") or 0),
            in_progress=int(raw.get("in_progress") or 0),
            waiting_username=int(raw.get("waiting_username") or 0),
            held=int(raw.get("held") or 0),
            stars=int(raw.get("stars") or 0),
            revenue_rub=round(float(raw.get("revenue_rub") or 0.0), 2),
            cost_usdt=round(float(raw.get("cost_usdt") or 0.0), 4),
            cost_rub=round(cost_rub, 2),
            profit_rub=round(float(raw.get("profit_rub") or 0.0), 2),
            loss_orders=int(raw.get("loss_orders") or 0),
            loss_rub=round(float(raw.get("loss_rub") or 0.0), 2),
            failed_potential_rub=round(float(raw.get("failed_potential_rub") or 0.0), 2),
            avg_profit_rub=round(float(raw.get("avg_profit_rub") or 0.0), 2),
            best_profit_rub=round(float(raw.get("best_profit_rub") or 0.0), 2),
        )

    async def window(self, label: str, hours: float) -> WindowStats:
        """Окно 'последние N часов' от текущего момента."""
        now = int(time.time())
        from_ts = now - int(hours * 3600)
        raw = await self.db.window_stats(start_ts=from_ts, end_ts=now)
        return self._apply_finance(raw, label, from_ts, now)

    async def day_window(self, label: str = "24 часа") -> WindowStats:
        return await self.window(label, 24)

    async def today_window(self, label: str = "Сегодня (с 00:00)") -> WindowStats:
        """Календарный день по локальному времени сервера."""
        now_dt = datetime.now()
        start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        from_ts = int(start_dt.timestamp())
        now_ts = int(time.time())
        raw = await self.db.window_stats(start_ts=from_ts, end_ts=now_ts)
        return self._apply_finance(raw, label, from_ts, now_ts)

    async def total_window(self, label: str = "Всего") -> WindowStats:
        """Вся история с момента запуска."""
        raw = await self.db.window_stats()
        now_ts = int(time.time())
        return self._apply_finance(raw, label, 0, now_ts)

    async def compute_all(self) -> list[WindowStats]:
        """Полный набор окон: 1ч, 2ч, 3ч, 4ч, 6ч, 24ч, Сегодня, Всего."""
        result: list[WindowStats] = []
        for label, hours in HOUR_WINDOWS:
            result.append(await self.window(label, hours))
        result.append(await self.today_window())
        result.append(await self.total_window())
        return result

    # ------------------------------------------------------------------ #
    # Отчёты: убытки / прибыль / затраты
    # ------------------------------------------------------------------ #

    async def loss_report(self, hours: float = 24.0) -> dict[str, Any]:
        """
        Анализ убытков за окно:
        - убыточные сделки (выручка < себестоимость)
        - проваленные заказы (потерянная выручка + списанные средства, если были)
        """
        now = int(time.time())
        from_ts = now - int(hours * 3600)
        raw = await self.db.window_stats(start_ts=from_ts, end_ts=now)
        failed_orders = await self.db.top_orders(limit=10, start_ts=from_ts, end_ts=now, failed_only=True)
        loss_makers = await self.db.top_orders(limit=10, start_ts=from_ts, end_ts=now)

        # Отдельно: completed с отрицательной прибылью
        conn_data = raw
        negative_profit = await self.db.get_negative_profit_orders(
            limit=10, start_ts=from_ts, end_ts=now
        )

        return {
            "window_label": f"последние {hours:g} ч",
            "from_ts": from_ts,
            "to_ts": now,
            "failed_count": int(conn_data.get("failed") or 0),
            "failed_potential_rub": round(float(conn_data.get("failed_potential_rub") or 0.0), 2),
            "loss_orders_count": int(conn_data.get("loss_orders") or 0),
            "loss_rub": round(float(conn_data.get("loss_rub") or 0.0), 2),
            "failed_orders": failed_orders,
            "negative_profit_orders": negative_profit,
            "top_orders": loss_makers,
        }

    # ------------------------------------------------------------------ #
    # Рендеринг
    # ------------------------------------------------------------------ #

    @staticmethod
    def _fmt_money(value: float) -> str:
        return f"{value:,.2f}".replace(",", " ")

    def render_text(self, windows: list[WindowStats]) -> str:
        """Таблица статистики для консоли."""
        lines: list[str] = []
        lines.append("=" * 118)
        lines.append(f"{'ОКНО':<16} | {'Заказы':>6} | {'OK':>4} | {'FAIL':>4} | {'В раб.':>6} | "
                     f"{'Звёзды':>8} | {'Выручка ₽':>12} | {'USDT':>8} | {'Затраты ₽':>12} | "
                     f"{'Прибыль ₽':>12} | {'Маржа':>6}")
        lines.append("-" * 118)

        for w in windows:
            lines.append(
                f"{w.label:<16} | {w.total:>6} | {w.completed:>4} | {w.failed:>4} | {w.in_progress:>6} | "
                f"{w.stars:>8} | {self._fmt_money(w.revenue_rub):>12} | {w.cost_usdt:>8.2f} | "
                f"{self._fmt_money(w.cost_rub):>12} | {self._fmt_money(w.profit_rub):>12} | "
                f"{(f'{w.margin_pct}%' if w.revenue_rub else '—'):>6}"
            )
        lines.append("=" * 118)

        last = windows[-1] if windows else None
        if last and last.total:
            lines.append(
                f"Убыточные сделки: {last.loss_orders} (−{self._fmt_money(abs(last.loss_rub))} ₽) | "
                f"Провалено заказов: {last.failed} (потерянная выручка: {self._fmt_money(last.failed_potential_rub)} ₽) | "
                f"Средняя прибыль/заказ: {self._fmt_money(last.avg_profit_rub)} ₽ | "
                f"Лучшая сделка: +{self._fmt_money(last.best_profit_rub)} ₽"
            )
        lines.append(
            f"Курс USDT: активный Вариант {self.active_variant} = {self.active_rate:.2f} ₽ "
            f"(Вариант 1: {self.rate_variant_1:.2f} ₽, Вариант 2 Whitebird: {self.rate_variant_2:.2f} ₽)"
        )
        return "\n".join(lines)

    def render_json(self, windows: list[WindowStats]) -> str:
        """JSON-дамп окон + курсов — стабильный контракт для GUI/внешних инструментов."""
        import json

        return json.dumps(
            {
                "generated_at": int(time.time()),
                "rate_variant_1": self.rate_variant_1,
                "rate_variant_2": self.rate_variant_2,
                "active_variant": self.active_variant,
                "active_rate": self.active_rate,
                "windows": [w.to_dict() for w in windows],
            },
            ensure_ascii=False,
        )

    def render_telegram(self, windows: list[WindowStats]) -> str:
        """Компактный HTML-отчёт для Telegram (1ч/24ч/Сегодня/Всего)."""
        def pick(label: str) -> WindowStats | None:
            for w in windows:
                if w.label == label:
                    return w
            return None

        blocks: list[str] = []
        blocks.append("📊 <b>СТАТИСТИКА AUTOSTARS</b>")
        blocks.append(f"Курс: В{self.active_variant} = {self.active_rate:.2f} ₽/USDT")

        for label in ("1 час", "24 часа", "Сегодня (с 00:00)", "Всего"):
            w = pick(label)
            if w is None:
                continue
            if w.is_empty:
                blocks.append(f"\n🕐 <b>{label}</b>: заказов нет")
                continue
            emoji = "🟢" if w.profit_rub >= 0 else "🔴"
            blocks.append(
                f"\n{emoji} <b>{label}</b> — заказов: {w.total} "
                f"(✔ {w.completed}, ✖ {w.failed}, ⏳ {w.in_progress}"
                + (f", ⛔ задержано {w.held}" if w.held else "")
                + ")"
            )
            blocks.append(
                f"   ⭐ Звёзды: {w.stars:,}".replace(",", " ")
                + f" | 💵 Выручка: {self._fmt_money(w.revenue_rub)} ₽"
            )
            blocks.append(
                f"   💸 Затраты: {w.cost_usdt:.2f} USDT ({self._fmt_money(w.cost_rub)} ₽)"
                + f" | 💰 Прибыль: <b>{self._fmt_money(w.profit_rub)} ₽</b>"
                + (f" | Маржа: {w.margin_pct}%" if w.revenue_rub else "")
            )
            if w.loss_orders or w.failed:
                blocks.append(
                    f"   ⚠️ Убыток: {w.loss_orders} сделок в минус (−{self._fmt_money(abs(w.loss_rub))} ₽), "
                    f"провалено: {w.failed} (−{self._fmt_money(w.failed_potential_rub)} ₽ выручки)"
                )

        return "\n".join(blocks)

    def render_full_report(self, windows: list[WindowStats],
                           top_orders: list[dict[str, Any]],
                           failed_orders: list[dict[str, Any]],
                           status_counts: dict[str, int]) -> str:
        """Развёрнутый ежедневный отчёт: статистика + P&L + убытки + топ + трекинг задач."""
        parts: list[str] = []
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        parts.append(f"📈 ПОЛНЫЙ ОТЧЁТ AUTOSTARS — {now_str}")
        parts.append("")
        parts.append(self.render_text(windows))
        parts.append("")

        today = next((w for w in windows if w.label == "24 часа"), None)
        if today and today.total:
            parts.append("🧾 P&L за 24 часа:")
            parts.append(f"   Выручка:   {self._fmt_money(today.revenue_rub):>14} ₽")
            parts.append(f"   Затраты:   {self._fmt_money(today.cost_rub):>14} ₽ ({today.cost_usdt:.2f} USDT)")
            parts.append(f"   Прибыль:   {self._fmt_money(today.profit_rub):>14} ₽")
            parts.append(f"   Маржа:     {today.margin_pct:>13}%")
            parts.append(
                f"   Убытки:    убыточные сделки {today.loss_orders} (−{self._fmt_money(abs(today.loss_rub))} ₽), "
                f"провалено {today.failed} (−{self._fmt_money(today.failed_potential_rub)} ₽)"
            )
            parts.append("")

        if top_orders:
            parts.append("🏆 Топ сделок по прибыли (за 24ч):")
            for o in top_orders[:5]:
                parts.append(
                    f"   #{o['order_id']} @{o.get('username') or '?'} — {int(o.get('quantity') or 0):,}".replace(",", " ")
                    + f" ⭐ | +{self._fmt_money(float(o.get('profit_rub') or 0))} ₽"
                )
            parts.append("")

        if failed_orders:
            parts.append("❌ Проваленные заказы (за 24ч):")
            for o in failed_orders[:5]:
                parts.append(
                    f"   #{o['order_id']} @{o.get('username') or '?'} — {o.get('status')} "
                    f"({o.get('error') or 'без причины'})"
                )
            parts.append("")

        parts.append("🗂 Трекинг задач (по статусам):")
        if status_counts:
            pretty = ", ".join(f"{k}: {v}" for k, v in sorted(status_counts.items()))
            parts.append(f"   {pretty}")
        else:
            parts.append("   заказов в базе нет")
        parts.append("")
        return "\n".join(parts)
