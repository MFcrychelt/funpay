"""Метрики и health-эндпоинт: `GET /metrics`, `GET /healthz` (выключено по умолчанию).

Зачем: цикл AutoStars обычно живёт на VPS рядом с Prometheus/node_exporter или
 просто в uptime-мониторинге. Просить «пришлите вывод --stats --json по cron»
— так себе эксплуатация.

Почему свой сервер на asyncio, а не aiohttp/flask:
  • зависимостей в ядре нет и не появится ради двух эндпоинтов;
  • ответ всегда один и тот же по форме (текст), тело не парсится;
  • слушает 127.0.0.1 по умолчанию, наружу ничего не торчит.

`/metrics` — текст в формате Prometheus (AUTOSTARS_METRICS_TOKEN задаёт
`Authorization: Bearer …`, если нужен доступ с другой машины через reverse-proxy).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("autostars.metrics")

CONTENT_TYPE_PROMETHEUS = "text/plain; version=0.0.4; charset=utf-8"


def render_prometheus(snapshot: dict[str, Any], started_at: float | None = None) -> str:
    """Снимок состояния → текст Prometheus. Ни одного внешнего вызова за данными."""
    lines: list[str] = []

    def metric(name: str, help_text: str, kind: str, value: Any) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.append(f"{name} {_fmt(value)}")

    counts = dict(snapshot.get("status_counts") or {})
    metric(
        "autostars_up",
        "1 — цикл работает (данные снимка свежие).",
        "gauge",
        1 if snapshot.get("running", True) else 0,
    )
    metric("autostars_orders_total", "Заказы в базе по статусам.", "gauge", sum(counts.values()))
    lines.append("# HELP autostars_orders_by_status Заказы по статусам.")
    lines.append("# TYPE autostars_orders_by_status gauge")
    for status, value in sorted(counts.items()):
        lines.append(f'autostars_orders_by_status{{status="{_label(status)}"}} {int(value)}')

    today = snapshot.get("today") or {}
    metric("autostars_today_revenue_rub", "Выручка ₽ с полуночи.", "gauge", today.get("revenue_rub", 0))
    metric("autostars_today_cost_usdt", "Затраты USDT с полуночи.", "gauge", today.get("cost_usdt", 0))
    metric("autostars_today_profit_rub", "Прибыль ₽ с полуночи.", "gauge", today.get("profit_rub", 0))
    metric("autostars_today_orders", "Заказов с полуночи.", "gauge", today.get("total", 0))

    gameau = snapshot.get("gameau") or {}
    if gameau.get("balance_usdt") is not None:
        metric("autostars_gameau_balance_usdt", "Баланс GAMEAU, USDT.", "gauge", gameau["balance_usdt"])
    if gameau.get("low_balance") is not None:
        metric("autostars_gameau_low_balance", "1 — баланс ниже порога.", "gauge", int(bool(gameau["low_balance"])))

    limits = snapshot.get("limits") or {}
    if limits:
        metric("autostars_today_spend_usdt", "Списано USDT с полуночи.", "gauge", limits.get("spent_usdt", 0))
        metric("autostars_daily_spend_limit_usdt", "Суточный лимит USDT (0 = выключен).", "gauge",
               limits.get("limit_usdt", 0))
        metric("autostars_held_orders", "Заказы, задержанные политикой выдачи.", "gauge", limits.get("held", 0))

    metric("autostars_paused", "1 — приём новых заказов на паузе (--pause).", "gauge",
           1 if snapshot.get("paused") else 0)
    metric("autostars_config_ok", "1 — конфигурация без критичных замечаний.", "gauge",
           1 if snapshot.get("config_ok", True) else 0)
    if started_at:
        metric("autostars_process_uptime_seconds", "Время работы цикла.", "gauge", int(time.time() - started_at))
    metric("autostars_snapshot_ts_seconds", "Время снимка (unix).", "gauge", int(snapshot.get("ts") or time.time()))
    return "\n".join(lines) + "\n"


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")


#: адреса, которые не открывают порт наружу — для них токен не обязателен
LOCAL_BINDS = frozenset({"", "127.0.0.1", "localhost", "::1"})


def is_local_bind(host: object) -> bool:
    """True, если слушаем только на машине (наружу из этого окна не постучаться)."""
    return str(host or "").strip().lower() in LOCAL_BINDS


class MetricsServer:
    """Крошечный HTTP-сервер: /metrics, /healthz, /status (JSON-снимок)."""

    def __init__(
        self,
        snapshot_provider: Callable[[], dict[str, Any]],
        *,
        host: str = "127.0.0.1",
        port: int = 9155,
        token: str = "",
        started_at: float | None = None,
    ) -> None:
        self._snapshot = snapshot_provider
        self.host = host
        self.port = int(port)
        self.token = (token or "").strip()
        if not is_local_bind(self.host) and not self.token:
            # открытый наружу снимок = читаемая витрина бизнеса; молча такое не поднимаем
            raise ValueError(
                f"метрики на {self.host!r} без METRICS_TOKEN — снимок читают все; "
                "задайте токен или оставьте 127.0.0.1"
            )
        self.started_at = started_at or time.time()
        self._server: asyncio.Server | None = None

    @property
    def bound_port(self) -> int:
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def serve(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        logger.info(f"Метрики: http://{self.host}:{self.bound_port}/metrics "
                    f"и /healthz (защита токеном: {'да' if self.token else 'нет'})")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = request.decode("latin-1").splitlines()
            parts = lines[0].split() if lines else []
            method = parts[0] if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"
            headers = {}
            for line in lines[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            path = path.split("?", 1)[0] or "/"

            if self.token and not self._authorized(headers):
                await self._respond(writer, 401, "text/plain; charset=utf-8", "unauthorized\n")
                return
            if method != "GET":
                await self._respond(writer, 405, "text/plain; charset=utf-8", "method not allowed\n")
                return

            snapshot: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                snapshot = dict(self._snapshot() or {})
            snapshot.setdefault("ts", int(time.time()))

            if path in ("/metrics", "/prometheus"):
                body = render_prometheus(snapshot, self.started_at)
                await self._respond(writer, 200, CONTENT_TYPE_PROMETHEUS, body)
            elif path in ("/healthz", "/health"):
                healthy = bool(snapshot.get("config_ok", True))
                body = "ok\n" if healthy else "degraded: " + ", ".join(snapshot.get("issues") or []) + "\n"
                await self._respond(writer, 200 if healthy else 503, "text/plain; charset=utf-8", body)
            elif path in ("/status", "/"):
                import json

                await self._respond(writer, 200, "application/json; charset=utf-8",
                                    json.dumps(snapshot, ensure_ascii=False, default=str))
            else:
                await self._respond(writer, 404, "text/plain; charset=utf-8", "not found\n")
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionResetError, ValueError):
            with contextlib.suppress(Exception):
                writer.close()
        except Exception as exc:  # сервер мониторинга не должен ронять цикл
            logger.debug(f"Metrics handler: {exc}")
            with contextlib.suppress(Exception):
                await self._respond(writer, 500, "text/plain; charset=utf-8", "error\n")
        finally:
            with contextlib.suppress(Exception):
                if not writer.is_closing():
                    await writer.drain()
                writer.close()

    def _authorized(self, headers: dict[str, str]) -> bool:
        supplied = headers.get("authorization", "")
        return supplied in (f"Bearer {self.token}", self.token)

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, content_type: str, body: str) -> None:
        reason = {200: "OK", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed",
                  500: "Internal Server Error", 503: "Service Unavailable"}.get(status, "OK")
        payload = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + payload)
        await writer.drain()
