"""История транзакций — хранит все выдачи звёзд в db.json.

Каждая запись содержит: order_id, username, stars, cost_usd, revenue_rub,
profit, date, gameau_order_id, status.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .logger_setup import get_logger

logger = get_logger("history")

DB_FILE = Path(__file__).resolve().parent.parent / "db.json"


class History:
    """Простая JSON-база транзакций."""

    def __init__(self, db_path: str | Path | None = None):
        self._path = Path(db_path) if db_path else DB_FILE
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"transactions": [], "stats": {
                "total_orders": 0, "total_stars": 0,
                "total_cost_usd": 0.0, "total_revenue_rub": 0.0,
                "total_profit_rub": 0.0,
            }}

    def _save(self) -> None:
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def add_transaction(self, *, order_id: str, username: str, stars: int,
                        cost_usd: float, revenue_rub: float = 0,
                        profit: float = 0, gameau_order_id: str = "",
                        status: str = "done", error: str = "") -> None:
        """Добавляет запись о выдаче."""
        tx = {
            "order_id": order_id,
            "username": username,
            "stars": stars,
            "cost_usd": round(cost_usd, 4),
            "revenue_rub": round(revenue_rub, 2),
            "profit": round(profit, 2),
            "gameau_order_id": gameau_order_id,
            "status": status,
            "error": error,
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": time.time(),
        }
        self._data.setdefault("transactions", []).append(tx)

        # Обновляем статистику
        stats = self._data.setdefault("stats", {})
        stats["total_orders"] = stats.get("total_orders", 0) + 1
        stats["total_stars"] = stats.get("total_stars", 0) + stars
        stats["total_cost_usd"] = round(stats.get("total_cost_usd", 0) + cost_usd, 4)
        stats["total_revenue_rub"] = round(stats.get("total_revenue_rub", 0) + revenue_rub, 2)
        stats["total_profit_rub"] = round(stats.get("total_profit_rub", 0) + profit, 2)

        self._save()
        logger.info("История: записана транзакция #%s — %d звёзд, прибыль %.2f ₽",
                     order_id, stars, profit)

    def get_transactions(self, limit: int = 100) -> list[dict]:
        """Возвращает последние N транзакций (новые первыми)."""
        txs = self._data.get("transactions", [])
        return list(reversed(txs[-limit:]))

    def get_stats(self) -> dict:
        """Возвращает агрегированную статистику."""
        return self._data.get("stats", {
            "total_orders": 0, "total_stars": 0,
            "total_cost_usd": 0.0, "total_revenue_rub": 0.0,
            "total_profit_rub": 0.0,
        })

    def get_today_stats(self) -> dict:
        """Статистика за сегодня."""
        today = time.strftime("%Y-%m-%d")
        txs = [t for t in self._data.get("transactions", [])
               if t.get("date", "").startswith(today)]
        return {
            "orders": len(txs),
            "stars": sum(t.get("stars", 0) for t in txs),
            "cost_usd": round(sum(t.get("cost_usd", 0) for t in txs), 4),
            "revenue_rub": round(sum(t.get("revenue_rub", 0) for t in txs), 2),
            "profit_rub": round(sum(t.get("profit", 0) for t in txs), 2),
        }
