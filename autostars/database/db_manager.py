"""Асинхронный менеджер базы данных SQLite (на aiosqlite)."""

from __future__ import annotations

import logging
from typing import Any, Optional
import aiosqlite

from .models import ALL_SCHEMAS

logger = logging.getLogger("autostars.db")


class DBManager:
    """Менеджер базы данных SQLite для AutoStars."""

    def __init__(self, db_path: str = "autostars.db"):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def get_connection(self) -> aiosqlite.Connection:
        """Возвращает активное соединение с базой данных."""
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
        return self._conn

    async def init_db(self) -> None:
        """Создает таблицы и индексы в базе данных, если они отсутствуют."""
        conn = await self.get_connection()
        for schema in ALL_SCHEMAS:
            await conn.execute(schema)
        await conn.commit()
        logger.info(f"База данных успешно инициализирована: {self.db_path}")

    async def is_order_processed(self, order_id: str) -> bool:
        """
        Проверяет, обрабатывался ли заказ ранее.
        Возвращает True, если заказ имеет статус COMPLETED или PROCESSING.
        """
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT status FROM orders WHERE order_id = ?", (str(order_id),)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            status = row["status"]
            return status in ("COMPLETED", "PROCESSING")

    async def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        """Получает запись заказа по order_id."""
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (str(order_id),)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def save_order_status(
        self,
        order_id: str,
        username: Optional[str],
        status: str,
        chat_node: Optional[str] = None,
        quantity: Optional[int] = None,
        price_rub: Optional[float] = None,
        cost_usdt: Optional[float] = None,
        profit_rub: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        """Сохраняет или обновляет статус заказа."""
        conn = await self.get_connection()
        order_id_str = str(order_id)
        existing = await self.get_order(order_id_str)

        if existing:
            query = """
            UPDATE orders SET
                username = COALESCE(?, username),
                chat_node = COALESCE(?, chat_node),
                quantity = COALESCE(?, quantity),
                price_rub = COALESCE(?, price_rub),
                cost_usdt = COALESCE(?, cost_usdt),
                profit_rub = COALESCE(?, profit_rub),
                status = ?,
                error = COALESCE(?, error),
                updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ?
            """
            await conn.execute(
                query,
                (
                    username,
                    chat_node,
                    quantity,
                    price_rub,
                    cost_usdt,
                    profit_rub,
                    status,
                    error,
                    order_id_str,
                ),
            )
        else:
            query = """
            INSERT INTO orders (
                order_id, username, chat_node, quantity, price_rub,
                cost_usdt, profit_rub, status, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
            await conn.execute(
                query,
                (
                    order_id_str,
                    username,
                    chat_node,
                    quantity or 1000,
                    price_rub or 0.0,
                    cost_usdt or 0.0,
                    profit_rub or 0.0,
                    status,
                    error,
                ),
            )
        await conn.commit()
        logger.debug(f"[ORDER {order_id}] Статус обновлен: {status} (username: {username})")

    async def log_idempotency(
        self,
        idempotency_key: str,
        order_id: str,
        request_payload: str = "",
        response_payload: str = "",
        status: str = "",
    ) -> None:
        """Сохраняет запись о ключе идемпотентности."""
        conn = await self.get_connection()
        await conn.execute(
            """
            INSERT OR REPLACE INTO idempotency_logs 
            (idempotency_key, order_id, request_payload, response_payload, status, created_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (idempotency_key, str(order_id), request_payload, response_payload, status),
        )
        await conn.execute(
            """
            INSERT OR IGNORE INTO idempotency_keys (idempotency_key, order_id, created_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (idempotency_key, str(order_id)),
        )
        await conn.commit()

    async def get_idempotency(self, idempotency_key: str) -> Optional[dict[str, Any]]:
        """Получает запись о ключе идемпотентности."""
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT * FROM idempotency_logs WHERE idempotency_key = ?",
            (idempotency_key,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Получает значение настройки."""
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        """Устанавливает значение настройки."""
        conn = await self.get_connection()
        await conn.execute(
            """
            INSERT OR REPLACE INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (key, value),
        )
        await conn.commit()

    async def get_recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        """Получает список последних заказов."""
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def close(self) -> None:
        """Закрывает соединение с базой данных."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
