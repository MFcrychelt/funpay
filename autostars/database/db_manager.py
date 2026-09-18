"""Асинхронный менеджер базы данных SQLite (на aiosqlite).

Добавлено в v2.1:
- Миграция legacy-баз (новые колонки orders, таблица task_events)
- Трекинг жизненного цикла заказов (task_events)
- Агрегированная статистика по временным окнам (1ч/2ч/3ч/4ч/6ч/24ч/день/всего)
- Определение "зависших" задач (PROCESSING без движения)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional
import aiosqlite

from .models import ALL_SCHEMAS, MIGRATE_ORDERS_COLUMNS, POST_MIGRATION_SCHEMAS

logger = logging.getLogger("autostars.db")

# Выraжение времени заказа: unix-эпоха (универсальная, не зависит от часового пояса)
TS_EXPR = "COALESCE(created_ts, CAST(strftime('%s', created_at) AS INTEGER))"
UPDATED_TS_EXPR = "COALESCE(updated_ts, CAST(strftime('%s', updated_at) AS INTEGER))"

IN_PROGRESS_STATUSES = ("PROCESSING", "WAITING_USERNAME")
TERMINAL_OK_STATUSES = ("COMPLETED",)
TERMINAL_FAIL_STATUSES = (
    "FAILED",
    "FAILED_LOW_BALANCE",
    "FAILED_PRICE_EXCEEDED",
    "FAILED_DELIVERY",
    "CANCELLED",
)
# Статусы, при которых допустим ручной ретрай заказа
RETRYABLE_STATUSES = TERMINAL_FAIL_STATUSES


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

    async def _migrate_orders_table(self, conn: aiosqlite.Connection) -> None:
        """Добавляет недостающие колонки в таблицу orders (legacy-базы)."""
        async with conn.execute("PRAGMA table_info(orders)") as cursor:
            rows = await cursor.fetchall()
        existing = {row["name"] for row in rows}
        for column, decl in MIGRATE_ORDERS_COLUMNS:
            if column not in existing:
                await conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {decl}")
                logger.info(f"Миграция БД: добавлена колонка orders.{column}")

    async def init_db(self) -> None:
        """Создает таблицы и индексы в базе данных, если они отсутствуют."""
        conn = await self.get_connection()
        for schema in ALL_SCHEMAS:
            await conn.execute(schema)
        # Миграция legacy-баз ДО создания индексов по новым колонкам
        await self._migrate_orders_table(conn)
        for schema in POST_MIGRATION_SCHEMAS:
            await conn.execute(schema)
        await conn.commit()
        logger.info(f"База данных успешно инициализирована: {self.db_path}")

    # ------------------------------------------------------------------ #
    # Заказы
    # ------------------------------------------------------------------ #

    async def is_order_processed(self, order_id: str) -> bool:
        """
        Знает ли система об этом заказе (есть ли запись в БД).

        Любая запись = заказ уже заведён в пайплайн: COMPLETED/PROCESSING —
        в работе или закрыт, WAITING_USERNAME — ждём ник от покупателя
        (подхватит чат-мониторинг, не спамим запрос заново), FAILED* — закрыт
        с ошибкой (повтор только через --retry-order / TG /retry).
        Это исключает повторную обработку и двойные ответы покупателю.
        """
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT 1 FROM orders WHERE order_id = ? LIMIT 1", (str(order_id),)
        ) as cursor:
            return await cursor.fetchone() is not None

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
        cost_rub: Optional[float] = None,
        profit_rub: Optional[float] = None,
        error: Optional[str] = None,
        gameau_order_id: Optional[str] = None,
    ) -> None:
        """Сохраняет или обновляет статус заказа."""
        conn = await self.get_connection()
        order_id_str = str(order_id)
        now_ts = int(time.time())
        existing = await self.get_order(order_id_str)

        if existing:
            query = """
            UPDATE orders SET
                gameau_order_id = COALESCE(?, gameau_order_id),
                username = COALESCE(?, username),
                chat_node = COALESCE(?, chat_node),
                quantity = COALESCE(?, quantity),
                price_rub = COALESCE(?, price_rub),
                cost_usdt = COALESCE(?, cost_usdt),
                cost_rub = COALESCE(?, cost_rub),
                profit_rub = COALESCE(?, profit_rub),
                status = ?,
                error = COALESCE(?, error),
                updated_ts = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ?
            """
            await conn.execute(
                query,
                (
                    gameau_order_id,
                    username,
                    chat_node,
                    quantity,
                    price_rub,
                    cost_usdt,
                    cost_rub,
                    profit_rub,
                    status,
                    error,
                    now_ts,
                    order_id_str,
                ),
            )
        else:
            query = """
            INSERT INTO orders (
                order_id, gameau_order_id, username, chat_node, quantity, price_rub,
                cost_usdt, cost_rub, profit_rub, status, error,
                created_ts, updated_ts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
            await conn.execute(
                query,
                (
                    order_id_str,
                    gameau_order_id,
                    username,
                    chat_node,
                    quantity or 1000,
                    price_rub or 0.0,
                    cost_usdt or 0.0,
                    cost_rub or 0.0,
                    profit_rub or 0.0,
                    status,
                    error,
                    now_ts,
                    now_ts,
                ),
            )
        await conn.commit()
        logger.debug(f"[ORDER {order_id}] Статус обновлен: {status} (username: {username})")

    async def get_orders_by_status(self, statuses: tuple[str, ...] | list[str],
                                   limit: int = 100) -> list[dict[str, Any]]:
        """Возвращает заказы со списанными статусами."""
        conn = await self.get_connection()
        placeholders = ",".join("?" for _ in statuses)
        async with conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) "
            f"ORDER BY {TS_EXPR} DESC LIMIT ?",
            (*statuses, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_in_progress_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        """Заказы, которые находятся в работе (PROCESSING / WAITING_USERNAME)."""
        return await self.get_orders_by_status(list(IN_PROGRESS_STATUSES), limit=limit)

    async def get_stuck_orders(self, stuck_seconds: int, limit: int = 100) -> list[dict[str, Any]]:
        """
        'Зависшие' задачи: статус в работе, но нет обновлений дольше stuck_seconds.
        """
        conn = await self.get_connection()
        threshold = int(time.time()) - int(stuck_seconds)
        placeholders = ",".join("?" for _ in IN_PROGRESS_STATUSES)
        async with conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) "
            f"AND {UPDATED_TS_EXPR} < ? ORDER BY {UPDATED_TS_EXPR} LIMIT ?",
            (*IN_PROGRESS_STATUSES, threshold, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Трекинг выполнения задач (журнал событий)
    # ------------------------------------------------------------------ #

    async def log_task_event(self, order_id: str, event: str, detail: str = "") -> None:
        """Записывает событие жизненного цикла заказа."""
        conn = await self.get_connection()
        await conn.execute(
            "INSERT INTO task_events (order_id, event, detail, ts_epoch, created_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (str(order_id), event, detail, int(time.time())),
        )
        await conn.commit()

    async def get_task_timeline(self, order_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Полный таймлайн событий по заказу (хронологически)."""
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT * FROM task_events WHERE order_id = ? ORDER BY ts_epoch ASC, id ASC LIMIT ?",
            (str(order_id), limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_recent_task_events(self, limit: int = 50,
                                     hours_back: Optional[float] = None) -> list[dict[str, Any]]:
        """Последние события по всем заказам."""
        conn = await self.get_connection()
        if hours_back is not None:
            threshold = int(time.time()) - int(hours_back * 3600)
            async with conn.execute(
                "SELECT * FROM task_events WHERE ts_epoch >= ? "
                "ORDER BY ts_epoch DESC, id DESC LIMIT ?",
                (threshold, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with conn.execute(
                "SELECT * FROM task_events ORDER BY ts_epoch DESC, id DESC LIMIT ?", (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def has_recent_task_event(self, order_id: str, event: str,
                                    minutes_back: float = 60.0) -> bool:
        """Было ли событие по заказу в последние минуты (анти-спам алертов)."""
        conn = await self.get_connection()
        threshold = int(time.time()) - int(minutes_back * 60)
        async with conn.execute(
            "SELECT 1 FROM task_events WHERE order_id = ? AND event = ? AND ts_epoch >= ? LIMIT 1",
            (str(order_id), event, threshold),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def get_status_counts(self) -> dict[str, int]:
        """Количество заказов по каждому статусу (общий трекинг)."""
        conn = await self.get_connection()
        async with conn.execute("SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status") as cursor:
            rows = await cursor.fetchall()
            return {row["status"]: int(row["cnt"]) for row in rows}

    # ------------------------------------------------------------------ #
    # Статистика по временным окнам
    # ------------------------------------------------------------------ #

    async def window_stats(self, start_ts: Optional[int] = None,
                           end_ts: Optional[int] = None) -> dict[str, Any]:
        """
        Агрегированная статистика по окну времени [start_ts, end_ts) в unix-эпохе.

        Окно задаётся локальным временем (не зависит от UTC-хранения created_at),
        поэтому все сравнения идут по unix-эпохе.

        Возвращает:
            total, completed, failed, in_progress, waiting_username,
            stars, revenue_rub, cost_usdt, cost_rub, profit_rub,
            loss_orders (completed с отрицательной прибылью), loss_rub,
            failed_potential_rub (потерянная выручка по проваленным заказам),
            avg_profit_rub, best_profit_rub
        """
        conn = await self.get_connection()

        where = ""
        params: tuple = ()
        if start_ts is not None:
            where = f"WHERE {TS_EXPR} >= ?"
            if end_ts is not None:
                where += f" AND {TS_EXPR} < ?"
                params = (int(start_ts), int(end_ts))
            else:
                params = (int(start_ts),)

        query = f"""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END), 0) AS completed,
            COALESCE(SUM(CASE WHEN status IN ('FAILED','FAILED_LOW_BALANCE','FAILED_PRICE_EXCEEDED','FAILED_DELIVERY','CANCELLED')
                              THEN 1 ELSE 0 END), 0) AS failed,
            COALESCE(SUM(CASE WHEN status IN ('PROCESSING','WAITING_USERNAME') THEN 1 ELSE 0 END), 0) AS in_progress,
            COALESCE(SUM(CASE WHEN status = 'WAITING_USERNAME' THEN 1 ELSE 0 END), 0) AS waiting_username,
            COALESCE(SUM(CASE WHEN status = 'COMPLETED' THEN quantity ELSE 0 END), 0) AS stars,
            COALESCE(SUM(CASE WHEN status = 'COMPLETED' THEN price_rub ELSE 0 END), 0.0) AS revenue_rub,
            COALESCE(SUM(CASE WHEN status = 'COMPLETED' THEN cost_usdt ELSE 0 END), 0.0) AS cost_usdt,
            COALESCE(SUM(CASE WHEN status = 'COMPLETED' THEN COALESCE(cost_rub, 0) ELSE 0 END), 0.0) AS cost_rub,
            COALESCE(SUM(CASE WHEN status = 'COMPLETED' THEN profit_rub ELSE 0 END), 0.0) AS profit_rub,
            COALESCE(SUM(CASE WHEN status = 'COMPLETED' AND profit_rub < 0 THEN 1 ELSE 0 END), 0) AS loss_orders,
            COALESCE(SUM(CASE WHEN status = 'COMPLETED' AND profit_rub < 0 THEN profit_rub ELSE 0 END), 0.0) AS loss_rub,
            COALESCE(SUM(CASE WHEN status IN ('FAILED','FAILED_LOW_BALANCE','FAILED_PRICE_EXCEEDED','FAILED_DELIVERY','CANCELLED')
                              THEN price_rub ELSE 0 END), 0.0) AS failed_potential_rub,
            COALESCE(MAX(CASE WHEN status = 'COMPLETED' THEN profit_rub ELSE NULL END), 0.0) AS best_profit_rub
        FROM orders
        {where}
        """
        async with conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
        data = dict(row) if row else {}

        total = int(data.get("total") or 0)
        completed = int(data.get("completed") or 0)
        profit_rub = float(data.get("profit_rub") or 0.0)
        data["total"] = total
        data["completed"] = completed
        data["failed"] = int(data.get("failed") or 0)
        data["in_progress"] = int(data.get("in_progress") or 0)
        data["waiting_username"] = int(data.get("waiting_username") or 0)
        data["stars"] = int(data.get("stars") or 0)
        data["revenue_rub"] = float(data.get("revenue_rub") or 0.0)
        data["cost_usdt"] = float(data.get("cost_usdt") or 0.0)
        data["cost_rub"] = float(data.get("cost_rub") or 0.0)
        data["profit_rub"] = profit_rub
        data["loss_orders"] = int(data.get("loss_orders") or 0)
        data["loss_rub"] = float(data.get("loss_rub") or 0.0)
        data["failed_potential_rub"] = float(data.get("failed_potential_rub") or 0.0)
        data["best_profit_rub"] = float(data.get("best_profit_rub") or 0.0)
        data["avg_profit_rub"] = round(profit_rub / completed, 2) if completed else 0.0
        return data

    async def top_orders(self, limit: int = 5, start_ts: Optional[int] = None,
                         end_ts: Optional[int] = None, failed_only: bool = False) -> list[dict[str, Any]]:
        """Топ заказов по прибыли (или проваленные) за окно."""
        conn = await self.get_connection()
        if failed_only:
            query = (
                "SELECT order_id, username, quantity, price_rub, cost_usdt, status, error "
                f"FROM orders WHERE status IN ('FAILED','FAILED_LOW_BALANCE','FAILED_PRICE_EXCEEDED','FAILED_DELIVERY','CANCELLED')"
            )
        else:
            query = (
                "SELECT order_id, username, quantity, price_rub, cost_usdt, profit_rub, status "
                "FROM orders WHERE status = 'COMPLETED'"
            )
        params: list = []
        if start_ts is not None:
            query += f" AND {TS_EXPR} >= ?"
            params.append(int(start_ts))
            if end_ts is not None:
                query += f" AND {TS_EXPR} < ?"
                params.append(int(end_ts))
        if failed_only:
            query += " ORDER BY " + TS_EXPR + " DESC LIMIT ?"
            params.append(limit)
        else:
            query += " ORDER BY profit_rub DESC LIMIT ?"
            params.append(limit)

        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_negative_profit_orders(self, limit: int = 10, start_ts: Optional[int] = None,
                                         end_ts: Optional[int] = None) -> list[dict[str, Any]]:
        """Завершённые убыточные сделки (прибыль < 0) за окно."""
        conn = await self.get_connection()
        query = (
            "SELECT order_id, username, quantity, price_rub, cost_usdt, profit_rub, status "
            "FROM orders WHERE status = 'COMPLETED' AND profit_rub < 0"
        )
        params: list = []
        if start_ts is not None:
            query += f" AND {TS_EXPR} >= ?"
            params.append(int(start_ts))
            if end_ts is not None:
                query += f" AND {TS_EXPR} < ?"
                params.append(int(end_ts))
        query += " ORDER BY profit_rub ASC LIMIT ?"
        params.append(limit)
        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Импотентность / настройки / прочее
    # ------------------------------------------------------------------ #

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
            f"SELECT * FROM orders ORDER BY {TS_EXPR} DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def close(self) -> None:
        """Закрывает соединение с базой данных."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
