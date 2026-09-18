"""Экспорт истории заказов и событий во внешние файлы (CSV / JSON).

Зачем: `--report` печатает красивый отчёт в терминал, но его не загонишь в
Excel, не приложишь к разбору инцидента и не сравнишь месяц помесячно. Здесь —
ровно то, что нужно для этих случаев, без внешних зависимостей.

Правила вывода:
  • CSV пишется в `utf-8-sig` (Excel на Windows иначе показывает кракозябры)
    и с CRLF;
  • разделитель `,`, кавычки по RFC 4180 — никаких `;`, чтобы файл одинаково
    читался в Excel, Numbers, pandas и `csv.reader`;
  • набор и порядок колонок фиксированы (`ORDER_COLUMNS`): выгрузка не должна
    «разъезжаться» в зависимости от содержимого;
  • файл пишется атомарно (`.tmp` → `os.replace`), чтобы обрыв не оставил
    половину отчёта;
  • SQL живёт в `DBManager.select_orders`, этот модуль только форматирует.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "EVENT_COLUMNS",
    "FAILED_LIKE",
    "ORDER_COLUMNS",
    "collect_events",
    "export_csv",
    "export_json",
    "parse_period",
    "rows_to_csv",
    "select_rows",
    "summarize_rows",
]

# «Неудачи» — для --failed: всё, что требует реакции человека.
FAILED_LIKE: frozenset[str] = frozenset(
    {
        "FAILED",
        "FAILED_LOW_BALANCE",
        "FAILED_PRICE_EXCEEDED",
        "FAILED_DELIVERY",
        "HOLD_MANUAL",
        "CANCELLED",
        "WAITING_USERNAME",
    }
)

#: Колонки соответствуют реальной схеме `orders` (см. database/models.py):
#: дашборды строятся на фиксированном наборе, поэтому «лишних» полей не добавляем.
ORDER_COLUMNS: tuple[str, ...] = (
    "order_id",
    "status",
    "username",
    "chat_node",
    "quantity",
    "price_rub",
    "cost_usdt",
    "cost_rub",
    "profit_rub",
    "gameau_order_id",
    "created_at",
    "updated_at",
    "created_ts",
    "updated_ts",
    "error",
)

EVENT_COLUMNS: tuple[str, ...] = ("order_id", "event", "detail", "ts_epoch", "created_at")

_UNITS = {"d": "days", "h": "hours", "m": "minutes", "w": "weeks"}


def parse_period(value: str | None, now: datetime | None = None) -> tuple[str, str] | None:
    """`7d`/`24h`/`30m`/`2w`/`2026-09-01`/`2026-09-01..2026-09-07` → (с, по).

    Границы — строки в том же формате, что хранятся в БД (UTC).
    Пустое значение и `all` → None (весь период).
    """
    if not value:
        return None
    raw = value.strip()
    if raw.lower() in ("all", "0", "none"):
        return None
    now = now or datetime.utcnow()

    def fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    if ".." in raw:
        start_s, _, end_s = raw.partition("..")
        start = _parse_date(start_s.strip())
        end = _parse_date(end_s.strip(), end_of_day=True)
        if start and end:
            return fmt(start), fmt(end)
        raise ValueError(f"не понял период {value!r}: по обе стороны от «..» нужна дата YYYY-MM-DD")

    unit = raw[-1].lower()
    if unit in _UNITS and raw[:-1].isdigit():
        step = timedelta(**{_UNITS[unit]: int(raw[:-1])})
        return fmt(now - step), fmt(now)

    single = _parse_date(raw)
    if single:
        return fmt(single), fmt(now)
    raise ValueError(
        f"не понял период {value!r}: примеры — 7d, 24h, 30m, 2w, "
        "2026-09-01, 2026-09-01..2026-09-07"
    )


def _parse_date(value: str, *, end_of_day: bool = False) -> datetime | None:
    for fmt_str in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt_str)
        except ValueError:
            continue
        if end_of_day and fmt_str == "%Y-%m-%d":
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt
    return None


def _clean(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def rows_to_csv(rows: Iterable[dict[str, Any]], columns: Sequence[str] = ORDER_COLUMNS) -> str:
    """Словари → текст CSV: CRLF, кавычки по RFC 4180, кодировка — на совести writers."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(list(columns))
    for row in rows:
        writer.writerow([_clean(row.get(col)) for col in columns])
    return buf.getvalue()


async def select_rows(
    db: Any,
    *,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    only_failed: bool = False,
    buyer: str | None = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Заказы по фильтрам выгрузки (параметры — как у `--export`)."""
    statuses: list[str] = []
    if status:
        statuses.extend(s.strip() for s in status.split(",") if s.strip())
    if only_failed:
        statuses = sorted(set(statuses) | set(FAILED_LIKE)) if statuses else sorted(FAILED_LIKE)
    return await db.select_orders(
        since=since,
        until=until,
        statuses=statuses or None,
        username=buyer,
        limit=limit,
    )


async def collect_events(db: Any, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Хронология событий по списку заказов (для второго файла выгрузки)."""
    events: list[dict[str, Any]] = []
    for row in rows:
        order_id = str(row.get("order_id") or "")
        if not order_id:
            continue
        try:
            timeline = await db.get_task_timeline(order_id, limit=1000)
        except Exception:  # у тестовых/ограниченных db может не быть этого метода
            continue
        events.extend(timeline)
    return events


def _atomic_write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


async def export_csv(
    db: Any,
    path: str | Path,
    *,
    with_events: bool = False,
    **filters: Any,
) -> dict[str, Any]:
    """Пишет CSV с заказами (и, опционально, CSV событий). Возвращает сводку."""
    target = Path(path)
    if target.suffix.lower() != ".csv":
        target = target.with_suffix(".csv")
    rows = await select_rows(db, **filters)
    written = _atomic_write(target, rows_to_csv(rows).encode("utf-8-sig"))
    summary: dict[str, Any] = {"path": str(written), "orders": len(rows), "format": "csv"}
    summary.update(summarize_rows(rows))
    if with_events:
        events = await collect_events(db, rows)
        events_path = target.with_name(f"{target.stem}.events.csv")
        _atomic_write(events_path, rows_to_csv(events, EVENT_COLUMNS).encode("utf-8-sig"))
        summary["events_path"] = str(events_path)
        summary["events"] = len(events)
    return summary


async def export_json(
    db: Any,
    path: str | Path,
    *,
    with_events: bool = False,
    **filters: Any,
) -> dict[str, Any]:
    """То же самое в JSON — для импорта в другой сервис и для отладки."""
    target = Path(path)
    if target.suffix.lower() != ".json":
        target = target.with_suffix(".json")
    rows = await select_rows(db, **filters)
    payload: list[dict[str, Any]] = [{col: row.get(col) for col in ORDER_COLUMNS} for row in rows]
    if with_events:
        events_by_order: dict[str, list[dict[str, Any]]] = {}
        for event in await collect_events(db, rows):
            events_by_order.setdefault(str(event.get("order_id")), []).append(
                {col: event.get(col) for col in EVENT_COLUMNS}
            )
        for item in payload:
            item["events"] = events_by_order.get(str(item["order_id"]), [])
    _atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    summary = {"path": str(target), "orders": len(payload), "format": "json"}
    summary.update(summarize_rows(payload))
    return summary


def summarize_rows(rows: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Сводка по выгрузке: сколько заказов, выручка ₽, себестоимость, прибыль ₽."""
    # cost_rub позволяет «сверить» убыточные сделки в Excel без пересчёта курса
    total = 0
    revenue = cost = profit = 0.0
    for row in rows:
        total += 1
        revenue += float(row.get("price_rub") or 0)
        cost += float(row.get("cost_usdt") or 0)
        profit += float(row.get("profit_rub") or 0)
    return {
        "orders": int(total),
        "revenue_rub": round(revenue, 2),
        "cost_usdt": round(cost, 2),
        "profit_rub": round(profit, 2),
    }
